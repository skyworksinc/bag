# SPDX-License-Identifier: Apache-2.0
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

"""This module defines classes and methods to cache previous simulation results."""

from __future__ import annotations

from typing import (
    TYPE_CHECKING, Optional, Type, Dict, List, Mapping, Any, Union, Tuple, Sequence, cast
)

import shutil
import filecmp
from pathlib import Path
from dataclasses import dataclass

from pybag.enum import DesignOutput, LogLevel
from pybag.core import FileLogger, gds_equal

from ..env import get_gds_layer_map, get_gds_object_map
from ..io.file import read_yaml, write_yaml
from ..util.logging import LoggingBase
from ..util.immutable import combine_hash
from ..util.importlib import import_class
from ..concurrent.util import GatherHelper
from ..concurrent.core import batch_async_task
from ..interface.database import DbAccess
from ..design.database import ModuleDB
from ..design.module import Module, PySchCellView
from ..layout.template import TemplateDB, TemplateBase
from .data import SimData
from .hdf5 import load_sim_data_hdf5
from .core import TestbenchManager
from .measure import MeasurementManager

if TYPE_CHECKING:
    from ..core import BagProject


@dataclass(frozen=True)
class DesignInstance:
    cell_name: str
    sch_master: Module
    lay_master: Optional[TemplateBase]
    netlist_path: Path
    cv_info_list: List[PySchCellView]

    @property
    def cache_name(self) -> str:
        return self.netlist_path.parent.name


@dataclass(frozen=True)
class SimResults:
    dut: Optional[DesignInstance]
    tbm: TestbenchManager
    data: SimData


@dataclass(frozen=True)
class MeasureResult:
    dut: Optional[DesignInstance]
    mm: MeasurementManager
    data: Mapping[str, Any]


class DesignDB(LoggingBase):
    """A classes that caches extracted netlists.
    """

    def __init__(self, root_dir: Path, log_file: str, db_access: DbAccess,
                 sim_netlist_type: DesignOutput, sch_db: ModuleDB, lay_db: TemplateDB,
                 extract: bool = False, gen_sch: bool = False, force_extract: bool = False,
                 log_level: LogLevel = LogLevel.DEBUG) -> None:
        LoggingBase.__init__(self, 'dsn_db', log_file, log_level=log_level)

        self._root_dir = root_dir
        self._db = db_access
        self._sim_type = sim_netlist_type
        self._sch_db = sch_db
        self._lay_db = lay_db
        self._extract = extract
        self._force_extract = force_extract
        self._gen_sch = gen_sch
        self._lay_map = get_gds_layer_map()
        self._obj_map = get_gds_object_map()

        root_dir.mkdir(parents=True, exist_ok=True)

        self._info_file = root_dir / 'cache.yaml'
        if self._info_file.exists():
            self._info_specs = read_yaml(self._info_file)
        else:
            self._info_specs = dict(
                cache={},
                cnt={},
            )
            write_yaml(self._info_file, self._info_specs)

        self._cache: Dict[int, List[str]] = self._info_specs['cache']
        self._cnt: Dict[str, int] = self._info_specs['cnt']

    @property
    def impl_lib(self) -> str:
        return self._sch_db.lib_name

    @property
    def sch_db(self) -> ModuleDB:
        return self._sch_db

    @property
    def gen_sch(self) -> bool:
        return self._gen_sch

    @property
    def extract(self) -> bool:
        return self._extract

    async def async_batch_design(self, dut_specs: Sequence[Mapping[str, Any]]
                                 ) -> Sequence[DesignInstance]:
        ans = []
        extract_set = set()
        gatherer = GatherHelper()
        for dut_info in dut_specs:
            dut, ext_path = await self._create_dut(**dut_info)
            ans.append(dut)
            if ext_path is not None and ext_path not in extract_set:
                extract_set.add(ext_path)
                impl_cell: str = dut_info['impl_cell']
                gatherer.append(self._extract_netlist(ext_path, impl_cell))

        if gatherer:
            await gatherer.gather_err()

        return ans

    async def async_new_design(self, impl_cell: str,
                               lay_cls: Union[Type[TemplateBase], Type[Module], str],
                               dut_params: Mapping[str, Any], extract: Optional[bool] = None,
                               name_prefix: str = '', name_suffix: str = '', flat: bool = False,
                               export_lay: bool = False) -> DesignInstance:
        dut, ext_path = await self._create_dut(impl_cell, lay_cls, dut_params, extract=extract,
                                               name_prefix=name_prefix, name_suffix=name_suffix,
                                               flat=flat, export_lay=export_lay)
        if ext_path is not None:
            await self._extract_netlist(ext_path, impl_cell)

        return dut

    def new_design(self, impl_cell: str, lay_cls: Union[Type[TemplateBase], Type[Module], str],
                   dut_params: Mapping[str, Any], extract: Optional[bool] = None) -> DesignInstance:
        coro = self.async_new_design(impl_cell, lay_cls, dut_params, extract=extract)
        results = batch_async_task([coro])
        if results is None:
            self.error('Design generation cancelled')

        ans = results[0]
        if isinstance(ans, Exception):
            raise ans
        return ans

    async def _create_dut(self, impl_cell: str,
                          dut_cls: Union[Type[TemplateBase], Type[Module], str],
                          dut_params: Mapping[str, Any], extract: Optional[bool] = None,
                          name_prefix: str = '', name_suffix: str = '', flat: bool = False,
                          export_lay: bool = False) -> Tuple[DesignInstance, Optional[Path]]:
        sim_ext = self._sim_type.extension
        exact_cell_names = {impl_cell}

        obj_cls = import_class(dut_cls)
        if issubclass(obj_cls, TemplateBase):
            self.log(f'Creating layout: {obj_cls.__name__}')
            lay_master = self._lay_db.new_template(obj_cls, params=dut_params)
            sch_params = lay_master.sch_params
            sch_cls = lay_master.get_schematic_class_inst()
            layout_hash = hash(lay_master.key)
            gds_file = str(self._root_dir / 'tmp.gds')
            if export_lay:
                self._lay_db.batch_layout([(lay_master, impl_cell)], output=DesignOutput.LAYOUT,
                                          name_prefix=name_prefix, name_suffix=name_suffix,
                                          exact_cell_names=exact_cell_names)
                await self._db.async_export_layout(self._lay_db.lib_name, impl_cell, gds_file)
            else:
                self._lay_db.batch_layout([(lay_master, impl_cell)], output=DesignOutput.GDS,
                                          fname=gds_file, name_prefix=name_prefix,
                                          name_suffix=name_suffix,
                                          exact_cell_names=exact_cell_names)
        else:
            if extract:
                raise ValueError('Cannot run extraction without layout.')

            lay_master = None
            sch_params = dut_params
            sch_cls = obj_cls
            layout_hash = 0
            gds_file = ''

        if extract and lay_master is None:
            raise ValueError('Cannot run extraction without layout.')

        self.log(f'Creating schematic: {sch_cls.__name__}')
        sch_master: Module = self._sch_db.new_master(sch_cls, params=sch_params)

        # create schematic netlist
        cdl_netlist = str(self._root_dir / 'tmp.cdl')
        cv_info_out = []
        sch_dut_list = [(sch_master, impl_cell)]
        self._sch_db.batch_schematic(sch_dut_list, output=DesignOutput.CDL,
                                     fname=cdl_netlist, cv_info_out=cv_info_out,
                                     name_prefix=name_prefix, name_suffix=name_suffix,
                                     exact_cell_names=exact_cell_names)
        if self._gen_sch:
            self._sch_db.batch_schematic(sch_dut_list,
                                         name_prefix=name_prefix, name_suffix=name_suffix,
                                         exact_cell_names=exact_cell_names)

        self.log('Check for existing netlist')
        hash_id = combine_hash(layout_hash, hash(sch_master.key))
        dir_list = self._cache.get(hash_id, None)
        if dir_list is None:
            dir_list = []
            self._cache[hash_id] = dir_list
            dir_path = self._generate_cell(impl_cell, cdl_netlist, gds_file)
            dir_list.append(dir_path.name)
            write_yaml(self._info_file, self._info_specs)
        else:
            dir_path = None
            for dir_name in dir_list:
                cur_dir = self._root_dir / dir_name
                if filecmp.cmp(cdl_netlist, cur_dir / 'netlist.cdl', shallow=False):
                    if (not gds_file) or gds_equal(gds_file, str(cur_dir / 'layout.gds')):
                        self.log('Found existing design, reusing DUT netlist.')
                        dir_path = cur_dir
                        break

            if dir_path is None:
                dir_path = self._generate_cell(impl_cell, cdl_netlist, gds_file)
                dir_list.append(dir_path.name)
                write_yaml(self._info_file, self._info_specs)

        if extract or (extract is None and self._extract):
            ans = dir_path / 'rcx.sp'
            if not ans.exists() or self._force_extract:
                extract_info = dir_path
            else:
                extract_info = None
        else:
            extract_info = None
            ans = dir_path / f'netlist.{sim_ext}'
            if not ans.exists():
                self._sch_db.batch_schematic(sch_dut_list, output=self._sim_type, fname=str(ans),
                                             name_prefix=name_prefix, name_suffix=name_suffix,
                                             exact_cell_names=exact_cell_names, flat=flat)

        return DesignInstance(impl_cell, sch_master, lay_master, ans, cv_info_out), extract_info

    async def _extract_netlist(self, dsn_dir: Path, impl_cell: str) -> None:
        impl_lib = self.impl_lib

        self.log('running LVS...')
        ext_dir = dsn_dir / 'rcx'
        lvs_passed, lvs_log = await self._db.async_run_lvs(impl_lib, impl_cell, run_rcx=True,
                                                           layout=str(dsn_dir / 'layout.gds'),
                                                           netlist=str(dsn_dir / 'netlist.cdl'),
                                                           run_dir=ext_dir)
        if lvs_passed:
            self.log('LVS passed!')
        else:
            self.error(f'LVS failed... log file: {lvs_log}')

        self.log('running RCX...')
        final_netlist, rcx_log = await self._db.async_run_rcx(impl_lib, impl_cell,
                                                              run_dir=ext_dir)
        if final_netlist:
            self.log('RCX passed!')
            shutil.copy(final_netlist, str(dsn_dir / 'rcx.sp'))
        else:
            self.error(f'RCX failed... log file: {rcx_log}')

    def _generate_cell(self, impl_cell: str, cdl_netlist: str, gds_file: str) -> Path:
        self.log('No existing design, generating netlist')
        cur_cnt = self._cnt.get(impl_cell, -1) + 1
        self._cnt[impl_cell] = cur_cnt
        dir_name = impl_cell if cur_cnt == 0 else f'{impl_cell}_{cur_cnt}'
        dir_path = self._root_dir / dir_name
        dir_path.mkdir(parents=True, exist_ok=True)

        shutil.move(cdl_netlist, str(dir_path / 'netlist.cdl'))
        if gds_file:
            shutil.move(gds_file, str(dir_path / 'layout.gds'))
        return dir_path


class SimulationDB(LoggingBase):
    """A classes that caches netlists, layouts, and simulation results.
    """

    def __init__(self, log_file: str, dsn_db: DesignDB, force_sim: bool = False,
                 precision: int = 6, log_level: LogLevel = LogLevel.DEBUG) -> None:
        LoggingBase.__init__(self, 'sim_db', log_file, log_level=log_level)

        self._dsn_db = dsn_db
        self._sim = self._dsn_db.sch_db.prj.sim_access
        self._force_sim = force_sim
        self._precision = precision

    @property
    def prj(self) -> BagProject:
        return self._dsn_db.sch_db.prj

    @property
    def precision(self) -> int:
        return self._precision

    @property
    def extract(self) -> bool:
        return self._dsn_db.extract

    def make_tbm(self, tbm_cls: Union[Type[TestbenchManager], str], tbm_specs: Mapping[str, Any],
                 work_dir: Optional[Path] = None, tb_name: str = '',
                 logger: Optional[FileLogger] = None) -> TestbenchManager:
        obj_cls = cast(Type[TestbenchManager], import_class(tbm_cls))

        if work_dir is None:
            work_dir = Path()
        if logger is None:
            logger = self.logger
        return obj_cls(self._sim, work_dir, tb_name, '', tbm_specs, None, None,
                       precision=self._precision, logger=logger)

    def make_mm(self, mm_cls: Union[Type[MeasurementManager], str], meas_specs: Mapping[str, Any]
                ) -> MeasurementManager:
        obj_cls = cast(Type[MeasurementManager], import_class(mm_cls))

        return obj_cls(meas_specs, self.log_file, log_level=self.log_level,
                       precision=self._precision)

    def new_design(self, impl_cell: str, lay_cls: Union[Type[TemplateBase], Type[Module], str],
                   dut_params: Mapping[str, Any], extract: Optional[bool] = None) -> DesignInstance:
        return self._dsn_db.new_design(impl_cell, lay_cls, dut_params, extract=extract)

    def simulate_tbm(self, sim_id: str, sim_dir: Path, dut: DesignInstance,
                     tbm_cls: Union[Type[TestbenchManager], str],
                     tb_params: Optional[Mapping[str, Any]], tbm_specs: Mapping[str, Any],
                     tb_name: str = '') -> SimResults:
        tbm = self.make_tbm(tbm_cls, tbm_specs)
        return self.simulate_tbm_obj(sim_id, sim_dir, dut, tbm, tb_params, tb_name=tb_name)

    def simulate_tbm_obj(self, sim_id: str, sim_dir: Path, dut: DesignInstance,
                         tbm: TestbenchManager, tb_params: Optional[Mapping[str, Any]],
                         tb_name: str = '') -> SimResults:
        coro = self.async_simulate_tbm_obj(sim_id, sim_dir, dut, tbm, tb_params, tb_name=tb_name)
        results = batch_async_task([coro])
        if results is None:
            self.error('Simulation cancelled')

        ans = results[0]
        if isinstance(ans, Exception):
            raise ans
        return ans

    def simulate_mm_obj(self, sim_id: str, sim_dir: Path, dut: DesignInstance,
                        mm: MeasurementManager) -> MeasureResult:
        coro = self.async_simulate_mm_obj(sim_id, sim_dir, dut, mm)
        results = batch_async_task([coro])
        if results is None:
            self.error('Measurement cancelled')

        ans = results[0]
        if isinstance(ans, Exception):
            raise ans
        return ans

    async def async_batch_design(self, dut_specs: Sequence[Mapping[str, Any]],
                                 ) -> Sequence[DesignInstance]:
        return await self._dsn_db.async_batch_design(dut_specs)

    async def async_new_design(self, impl_cell: str,
                               lay_cls: Union[Type[TemplateBase], Type[Module], str],
                               dut_params: Mapping[str, Any], extract: Optional[bool] = None,
                               name_prefix: str = '', name_suffix: str = '',
                               flat: bool = False, export_lay: bool = False) -> DesignInstance:
        return await self._dsn_db.async_new_design(impl_cell, lay_cls, dut_params, extract=extract,
                                                   name_prefix=name_prefix, export_lay=export_lay,
                                                   name_suffix=name_suffix, flat=flat)

    async def async_simulate_tbm_obj(self, sim_id: str, sim_dir: Path,
                                     dut: Optional[DesignInstance], tbm: TestbenchManager,
                                     tb_params: Optional[Mapping[str, Any]],
                                     tb_name: str = '') -> SimResults:
        if not tb_name:
            tb_name = sim_id

        sch_db = self._dsn_db.sch_db
        impl_lib = sch_db.lib_name
        tbm.update(work_dir=sim_dir, tb_name=tb_name, sim=self._sim)

        # update tb_params
        if dut is None:
            cv_info_list = []
            dut_netlist = None
            dut_mtime = None
        else:
            cv_info_list = dut.cv_info_list
            dut_netlist = dut.netlist_path
            dut_mtime = dut.netlist_path.stat().st_mtime
            tb_params = _set_dut(tb_params, impl_lib, dut.cell_name)
        sim_netlist = tbm.sim_netlist_path
        sim_data_path = self._sim.get_sim_file(sim_dir, sim_id)

        # check if DUT netlist is updated
        if sim_data_path.exists():
            force_sim = self._force_sim
            data_mtime = sim_data_path.stat().st_mtime
        else:
            force_sim = True
            data_mtime = -1

        # save previous simulation netlist, if exists
        prev_netlist = sim_netlist.with_name(sim_netlist.name + '.bak')
        if sim_netlist.exists():
            shutil.move(str(sim_netlist), str(prev_netlist))
        elif prev_netlist.exists():
            prev_netlist.unlink()

        self.log(f'Configuring testbench manager {tbm.__class__.__name__}')
        tbm.setup(sch_db, tb_params, cv_info_list, dut_netlist, gen_sch=self._dsn_db.gen_sch)
        if not sim_netlist.is_file():
            self.error(f'Cannot find simulation netlist: {sim_netlist}')

        # determine whether to run simulation
        if (not force_sim and prev_netlist.exists() and
                filecmp.cmp(sim_netlist, prev_netlist, shallow=False)):
            # simulation netlist is not modified
            if dut_mtime is not None and dut_mtime >= data_mtime:
                # DUT netlist is modified, re-run simulation
                self.log(f'DUT netlist mtime = {dut_mtime} >= sim data mtime = {data_mtime}, '
                         'Re-running simulation.')
                run_sim = True
            else:
                run_sim = False
        else:
            run_sim = True

        if run_sim:
            self.log(f'Simulating netlist: {sim_netlist}')
            await self._sim.async_run_simulation(sim_netlist, sim_id)
            self.log(f'Finished simulating {sim_netlist}')
        else:
            self.log('Returning previous simulation data')

        return SimResults(dut, tbm, load_sim_data_hdf5(sim_data_path))

    async def async_simulate_mm_obj(self, sim_id: str, sim_dir: Path, dut: Optional[DesignInstance],
                                    mm: MeasurementManager) -> MeasureResult:
        result = await mm.async_measure_performance(sim_id, sim_dir, self, dut)
        return MeasureResult(dut, mm, result)


def _set_dut(tb_params: Optional[Mapping[str, Any]], dut_lib: str, dut_cell: str
             ) -> Optional[Mapping[str, Any]]:
    """Returns a copy of the testbench parameters dictionary with DUT instantiated.

    This method updates the testbench parameters dictionary so that the DUT is instantiated
    statically in the inner-most wrapper.
    """
    if tb_params is None:
        return tb_params

    ans = {k: v for k, v in tb_params.items()}
    dut_params: Optional[Mapping[str, Any]] = tb_params.get('dut_params', None)
    if dut_params is None:
        ans['dut_lib'] = dut_lib
        ans['dut_cell'] = dut_cell
    else:
        ans['dut_params'] = _set_dut(dut_params, dut_lib, dut_cell)
    return ans
