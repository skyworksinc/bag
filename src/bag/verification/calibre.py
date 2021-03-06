# SPDX-License-Identifier: BSD-3-Clause AND Apache-2.0
# Copyright 2018 Regents of the University of California
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

# Copyright 2019 Blue Cheetah Analog Design Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""This module implements LVS/RCX using Calibre and stream out from Virtuoso.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Union, List, Tuple, Dict, Any, Sequence, Callable

from enum import Enum
from pathlib import Path

from ..io import write_file, read_file

from .virtuoso import VirtuosoChecker, all_pass_callback

if TYPE_CHECKING:
    from .base import FlowInfo


class RCXMode(Enum):
    xrc = 0
    xact = 1
    qrc = 2


class Calibre(VirtuosoChecker):
    """A subclass of VirtuosoChecker that uses Calibre for verification.

    Parameters
    ----------
    tmp_dir : str
        temporary directory to save files in.
    root_dir : Dict[str, str]
        dictionary of root run directories.
    template : Dict[str, str]
        dictionary of SVRF jinja template files.
    env_vars: Dict[str, Dict[str, str]]
        dictionary of environment variables.
    params : Dict[str, Dict[str, Any]]
        dictionary of default flow parameters.
    rcx_program : str
        the extraction program name.
    max_workers : int
        maximum number of sub-processes BAG can launch.
    source_added_file : str
        the Calibre source.added file location.  Environment variable is supported.
        If empty (default), this is not configured.
    cancel_timeout_ms : int
        cancel timeout in milliseconds.
    enable_color : bool
        True to enable coloring in GDS export.
    """

    def __init__(self, tmp_dir: str, root_dir: Dict[str, str], template: Dict[str, str],
                 env_vars: Dict[str, Dict[str, str]], link_files: Dict[str, List[str]],
                 params: Dict[str, Dict[str, Any]], rcx_program: str = 'pex', max_workers: int = 0,
                 source_added_file: str = '', cancel_timeout_ms: int = 10000,
                 enable_color: bool = False) -> None:
        VirtuosoChecker.__init__(self, tmp_dir, root_dir, template, env_vars, link_files,
                                 params, max_workers, source_added_file, cancel_timeout_ms,
                                 enable_color)

        self._rcx_mode: RCXMode = RCXMode[rcx_program]

    def get_rcx_netlists(self, lib_name: str, cell_name: str) -> List[str]:
        """Returns a list of generated extraction netlist file names.

        Parameters
        ----------
        lib_name : str
            library name.
        cell_name : str
            cell_name

        Returns
        -------
        netlists : List[str]
            a list of generated extraction netlist file names.  The first index is the main netlist.
        """
        # PVS generate schematic cellviews directly.
        if self._rcx_mode is RCXMode.qrc:
            return [f'{cell_name}.spf']
        else:
            return [f'{cell_name}.pex.netlist',
                    f'{cell_name}.pex.netlist.pex',
                    f'{cell_name}.pex.netlist.{cell_name}.pxi',
                    ]

    def setup_drc_flow(self, lib_name: str, cell_name: str, lay_view: str = 'layout',
                       layout: str = '', params: Optional[Dict[str, Any]] = None,
                       run_dir: Union[str, Path] = '') -> Sequence[FlowInfo]:
        cmd = ['calibre', '-drc', '-hier', None]
        return self._setup_flow_helper(lib_name, cell_name, layout, None, lay_view,
                                       '', params, 'drc', cmd, _drc_passed_check, run_dir)

    def setup_lvs_flow(self, lib_name: str, cell_name: str, sch_view: str = 'schematic',
                       lay_view: str = 'layout', layout: str = '', netlist: str = '',
                       params: Optional[Dict[str, Any]] = None, run_rcx: bool = False,
                       run_dir: Union[str, Path] = '') -> Sequence[FlowInfo]:
        cmd = ['calibre', '-lvs', '-hier', None]
        mode = 'lvs_rcx' if run_rcx else 'lvs'
        return self._setup_flow_helper(lib_name, cell_name, layout, netlist, lay_view,
                                       sch_view, params, mode, cmd, _lvs_passed_check, run_dir)

    def setup_rcx_flow(self, lib_name: str, cell_name: str,
                       params: Optional[Dict[str, Any]] = None, run_dir: Union[str, Path] = ''
                       ) -> Sequence[FlowInfo]:
        # noinspection PyUnusedLocal
        def _rcx_passed_check(retcode: int, log_file: str) -> Tuple[str, str]:
            fpath = Path(log_file).resolve()
            out_file: Path = fpath.parent
            out_file = out_file.joinpath(f'{cell_name}.spf')
            if not out_file.is_file():
                return '', ''

            return str(out_file), log_file

        cmd = ['qrc', '-64', '-cmd', None]
        flow_list = self._setup_flow_helper(lib_name, cell_name, None, None, '',
                                            '', params, 'rcx', cmd, _rcx_passed_check, run_dir)

        _, log_fname, env, dir_name, _ = flow_list[-1]
        query_log = Path(log_fname).with_name('bag_query.log')
        cmd = ['calibre', '-query_input', 'query.cmd', '-query', 'svdb']
        flow_list.insert(len(flow_list) - 2,
                         (cmd, str(query_log), env, dir_name, all_pass_callback))
        return flow_list

    def _setup_flow_helper(self, lib_name: str, cell_name: str, layout: Optional[str],
                           netlist: Optional[str], lay_view: str, sch_view: str,
                           user_params: Optional[Dict[str, Any]], mode: str, run_cmd: List[str],
                           check_fun: Callable[[Optional[int], str], Any],
                           run_dir_override: Union[str, Path]) -> List[FlowInfo]:
        tmp = self.setup_job(mode, lib_name, cell_name, layout, netlist, lay_view,
                             sch_view, user_params, run_dir_override)
        flow_list, run_dir, run_env, params, ctl_params = tmp

        # generate new control file
        ctl_path = self._make_control_file(mode, run_dir, ctl_params)
        run_cmd[-1] = str(ctl_path)

        log_path = run_dir / f'bag_{mode}.log'
        flow_list.append((run_cmd, str(log_path), run_env, str(run_dir), check_fun))

        return flow_list

    def _make_control_file(self, mode: str, run_dir: Path, ctl_params: Dict[str, str]) -> Path:
        ctl_path = run_dir / f'bag_{mode}.ctrl'
        temp = self.get_control_template(mode)
        content = temp.render(**ctl_params)
        write_file(ctl_path, content)

        return ctl_path


# noinspection PyUnusedLocal
def _drc_passed_check(retcode: int, log_file: str) -> Tuple[bool, str]:
    """Check if DRC passed

    Parameters
    ----------
    retcode : int
        return code of the LVS process.
    log_file : str
        log file name.

    Returns
    -------
    success : bool
        True if LVS passed.
    log_file : str
        the log file name.
    """
    fpath = Path(log_file)
    if not fpath.is_file():
        return False, ''

    cmd_output = read_file(fpath)
    test_str = '--- TOTAL RESULTS GENERATED = 0 (0)'
    return test_str in cmd_output, log_file


# noinspection PyUnusedLocal
def _lvs_passed_check(retcode: int, log_file: str) -> Tuple[bool, str]:
    """Check if LVS passed

    Parameters
    ----------
    retcode : int
        return code of the LVS process.
    log_file : str
        log file name.

    Returns
    -------
    success : bool
        True if LVS passed.
    log_file : str
        the log file name.
    """
    fpath = Path(log_file)
    if not fpath.is_file():
        return False, ''

    cmd_output = read_file(fpath)
    test_str = 'LVS completed. CORRECT. See report file:'
    return test_str in cmd_output, log_file
