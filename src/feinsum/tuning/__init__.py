"""
.. autoclass:: IntParameter
.. autoclass:: BoolParameter

.. autofunction:: autotune
.. autofunction:: transform_param
.. autofunction:: einsum_arg
"""
import abc
import os
import sqlite3
import numpy as np
import pyopencl as cl
import loopy as lp
import opentuner

from typing import Callable, Any, Tuple, Mapping, Optional, Sequence
from dataclasses import dataclass
from functools import cached_property, cache
from feinsum.einsum import (FusedEinsum, IntegralT, ShapeComponentT,
                            INT_CLASSES)
from feinsum.sql_utils import DEFAULT_DB
from feinsum.typing import TransformT
import logging
logger = logging.getLogger(__name__)


# {{{ supported tuning parameters

class TuningParameter(abc.ABC):  # noqa: B024
    pass


@dataclass(frozen=True, init=False)
class IntParameter(TuningParameter):
    """
    A parameter that takes values in the range ``[low, high]``.
    """
    low: IntegralT
    high: IntegralT

    def __init__(self, low: ShapeComponentT, high: ShapeComponentT):
        if not isinstance(low, INT_CLASSES):
            raise ValueError("low must be an integer")

        if not isinstance(high, INT_CLASSES):
            raise ValueError("high must be an integer")

        object.__setattr__(self, "low", low)
        object.__setattr__(self, "high", high)


@dataclass(frozen=True)
class BoolParameter(TuningParameter):
    """
    A parameter that can be either *True* or *False*.
    """

# }}}


# {{{ einsum_arg/transform_arg

@dataclass(frozen=True, repr=True)
class einsum_arg:  # noqa: N801
    """
    Decorate to a template transformation to inform
    :func:`autotune` about a static argument to the transformation
    implementation.

    :param arg_name: Name of the argument in the template transform
        that is to be statically set.
    :param param_getter: A callable that expects an einsum and returns the
        value of the static argument.
    """
    var_name: str
    func: Callable[[FusedEinsum], Any]

    def __call__(self, fn: Callable[..., Any]) -> "ParametrizedTransform":
        if isinstance(fn, ParametrizedTransform):
            return ParametrizedTransform(
                fn.transform,
                (self,) + fn.einsum_derivative_args,
                fn.transform_params)
        else:
            from functools import cache
            return ParametrizedTransform(cache(fn), (self,), ())


@dataclass(frozen=True, repr=True)
class transform_param:  # noqa: N801
    """
    Decorate to a template transformation to inform
    :func:`autotune` about the parameter space.

    :param arg_name: Name of the argument in the template
        transformation to be parametrized.
    :param param_getter: A callable that expects an einsum and returns the
        parameter space for that einsum.
    """
    var_name: str
    func: Callable[[FusedEinsum], TuningParameter]

    def __call__(self,
                 fn: Callable[..., lp.TranslationUnit]) -> "ParametrizedTransform":
        if isinstance(fn, ParametrizedTransform):
            return ParametrizedTransform(
                fn.transform,
                fn.einsum_derivative_args,
                (self,) + fn.transform_params)
        else:
            from functools import cache
            return ParametrizedTransform(cache(fn), (), (self,))


@dataclass(frozen=True, repr=True)
class ParametrizedTransform:
    transform: Callable[..., lp.TranslationUnit]
    einsum_derivative_args: Tuple[einsum_arg, ...]
    transform_params: Tuple[transform_param, ...]

    def __call__(self, *args: Any, **kwargs: Any) -> lp.TranslationUnit:
        return self.transform(*args, **kwargs)

    def bind_args(self,
                  einsum: FusedEinsum,
                  **transform_args: Any) -> TransformT:
        """
        Binds *transform_args* to *self* and returns a python callable
        to the corresponding instance in the self space.
        """
        from functools import partial

        py_clbl = partial(self.transform,
                          **{arg.var_name: arg.func(einsum)
                             for arg in self.einsum_derivative_args},
                          **transform_args)
        return py_clbl

# }}}


@cache
def _get_impls_path() -> str:
    import importlib.util
    feinsum_spec = importlib.util.find_spec("feinsum")
    assert feinsum_spec is not None
    assert feinsum_spec.origin is not None

    return os.path.abspath(
        os.path.join(feinsum_spec.origin,
                     os.path.pardir, "tuning", "impls"))


class ConfigurationNotInDBError(LookupError):
    pass


def get_transform_func_from_module_path(module_path: str) -> ParametrizedTransform:
    from importlib import util
    _, filename = os.path.split(module_path)

    assert filename.endswith(".py")

    spec = util.spec_from_file_location(filename[:-3], module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not import 'transform' function"
                           f" from {module_path}.")
    transform_module = util.module_from_spec(spec)
    spec.loader.exec_module(transform_module)
    transform_obj = transform_module.transform

    if isinstance(transform_obj, ParametrizedTransform):
        return transform_obj
    else:
        assert callable(transform_obj)
        return ParametrizedTransform(transform_obj, (), ())


# {{{ Opentuner entrypoint

# type-ignored as we are sub-classing Any type.
class OpentunerTuner(opentuner.MeasurementInterface):  # type: ignore[misc]
    def __init__(self,
                 args: Any,
                 einsum: FusedEinsum,
                 cl_ctx: cl.Context,
                 module_path: str,
                 long_dim_length: int,
                 db_path: str,
                 *,
                 # Args to super class ->
                 project_name: Optional[str] = None,
                 program_name: str = "unknown",
                 program_version: str = "unknown",
                 manipulator: Optional[Any] = None,
                 objective: Optional[Any] = None,
                 input_manager: Optional[Any] = None) -> None:
        from feinsum.canonicalization import canonicalize_einsum
        super().__init__(args=args,
                         project_name=project_name,
                         program_name=program_name,
                         program_version=program_version,
                         manipulator=manipulator,
                         objective=objective,
                         input_manager=input_manager)

        self.einsum = canonicalize_einsum(einsum)
        self.cl_ctx = cl_ctx
        self.module_path = module_path
        self.long_dim_length = long_dim_length
        self.db_path = db_path

    @cached_property
    def sql_table_name(self) -> str:
        from feinsum.sql_utils import dump_tablename
        dev, = self.cl_ctx.devices
        return dump_tablename(dev)

    @cached_property
    def transform_space_id(self) -> str:
        dirpath, filepath = os.path.split(self.module_path)

        if dirpath == _get_impls_path():
            return filepath
        else:
            return self.module_path

    @cached_property
    def conn(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path)
        cursor = db.cursor()
        cursor.execute(" SELECT name FROM sqlite_master"
                       f" WHERE (type='table' AND name='{self.sql_table_name}');")

        if not cursor.fetchall():
            # device table not available
            logger.info(f"Table {self.sql_table_name} not in DB, creating one.")
            cursor.execute(f"CREATE TABLE {self.sql_table_name} ("
                           " ID INTEGER PRIMARY KEY AUTOINCREMENT,"
                           " subscripts TEXT,"
                           " index_to_length TEXT,"
                           " use_matrix TEXT,"
                           " value_to_dtype TEXT,"
                           " transform_id TEXT,"
                           " transform_params TEXT,"
                           " runtime_in_sec REAL,"
                           " compiler_version TEXT,"
                           " giga_op_info TEXT,"
                           " timestamp TEXT"
                           ")")
        return db

    @cached_property
    def transform_func(self) -> ParametrizedTransform:
        return get_transform_func_from_module_path(self.module_path)

    def manipulator(self) -> "opentuner.ConfigurationManipulator":
        from opentuner import ConfigurationManipulator
        from opentuner.search.manipulator import BooleanParameter
        manipulator = ConfigurationManipulator()

        for param in self.transform_func.transform_params:
            tuning_param = param.func(self.einsum)
            if isinstance(tuning_param, IntParameter):
                manipulator.add_parameter(
                    opentuner.IntegerParameter(param.var_name,
                                               tuning_param.low,
                                               tuning_param.high))
            elif isinstance(tuning_param, BoolParameter):
                manipulator.add_parameter(BooleanParameter(param.var_name))
            else:
                raise NotImplementedError(f"Parameter: {param}.")

        return manipulator

    def seed_configurations(self) -> Sequence[Mapping[str, Any]]:
        import json
        from feinsum.sql_utils import (dump_index_to_length,
                                       dump_use_matrix,
                                       dump_value_to_dtype,
                                       dump_op_info,
                                       )

        cursor = self.conn.cursor()
        subscripts = self.einsum.get_subscripts()
        index_to_length = dump_index_to_length(self.einsum)
        use_matrix = dump_use_matrix(self.einsum)
        op_info = dump_op_info(self.einsum, self.long_dim_length)
        value_to_dtype = dump_value_to_dtype(self.einsum)

        cursor.execute(" SELECT"
                       "     transform_params"
                       "  FROM "
                       f"    {self.sql_table_name}"
                       " WHERE ("
                       "    transform_id = ?"
                       "    AND subscripts = ?"
                       "    AND index_to_length = ?"
                       "    AND use_matrix = ?"
                       "    AND value_to_dtype = ?"
                       "    AND giga_op_info = ?"
                       ");",
                       (self.transform_space_id, subscripts, index_to_length,
                        use_matrix, value_to_dtype, op_info))

        return [
            json.loads(transform_params[0])
            for transform_params in cursor.fetchall()
        ]

    def query_from_db(self, parameters: Mapping[str, Any]) -> float:
        import json
        from feinsum.sql_utils import (dump_index_to_length,
                                       dump_use_matrix,
                                       dump_op_info,
                                       dump_value_to_dtype)

        cursor = self.conn.cursor()
        subscripts = self.einsum.get_subscripts()
        index_to_length = dump_index_to_length(self.einsum)
        use_matrix = dump_use_matrix(self.einsum)
        value_to_dtype = dump_value_to_dtype(self.einsum)
        transform_params_str = json.dumps(parameters, sort_keys=True)
        op_info = dump_op_info(self.einsum, self.long_dim_length)

        cursor.execute(" SELECT"
                        "     runtime_in_sec"
                       "  FROM "
                       f"    {self.sql_table_name}"
                       " WHERE ("
                       f"    subscripts = ?"
                       f"    AND index_to_length = ?"
                       f"    AND use_matrix = ?"
                       f"    AND value_to_dtype = ?"
                       f"    AND transform_params = ?"
                       f"    AND giga_op_info = ?"
                       ");",
                       (subscripts, index_to_length, use_matrix,
                        value_to_dtype, transform_params_str, op_info,))
        stored_results = cursor.fetchall()

        if not stored_results:
            raise ConfigurationNotInDBError
        else:
            # mypy is probably right as the type parsed from the sql is not
            # obvious to be a float.
            return min(stored_result[0]  # type: ignore[no-any-return]
                       for stored_result in stored_results)

    def record_into_db(self, runtime: float, parameters: Mapping[str, Any]) -> None:
        import json
        from feinsum.sql_utils import (dump_index_to_length,
                                       dump_use_matrix,
                                       dump_value_to_dtype,
                                       dump_cl_version,
                                       dump_op_info)

        cursor = self.conn.cursor()
        subscripts = self.einsum.get_subscripts()
        index_to_length = dump_index_to_length(self.einsum)
        use_matrix = dump_use_matrix(self.einsum)
        value_to_dtype = dump_value_to_dtype(self.einsum)
        transform_params_str = json.dumps(parameters, sort_keys=True)
        cl_device, = self.cl_ctx.devices
        compiler_version = dump_cl_version(cl_device)
        op_info = dump_op_info(self.einsum, long_dim_length=self.long_dim_length)

        # {{{ compute timestamp in Chicago

        import pytz
        from datetime import datetime

        timestamp = (datetime
                     .now(pytz.timezone("America/Chicago"))
                     .strftime("%Y_%m_%d_%H%M%S"))

        # }}}

        cursor.execute(f"INSERT INTO {self.sql_table_name}"
                       " (subscripts, index_to_length, use_matrix,"
                       "  value_to_dtype, transform_id,"
                       "  transform_params, runtime_in_sec,"
                       "  compiler_version, giga_op_info, timestamp)"
                       " VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (subscripts, index_to_length, use_matrix,
                       value_to_dtype, self.transform_space_id,
                       transform_params_str, runtime, compiler_version,
                       op_info, timestamp))

        self.conn.commit()

    def run(self, desired_result: "opentuner.DesiredResult",
            input: Any, limit: Any) -> "opentuner.Result":
        from feinsum.measure import (timeit,
                                     stringify_comparison_vs_roofline)
        from feinsum.diagnostics import InvalidParameterError

        cfg = desired_result.configuration.data

        logger.info(cfg)

        # {{{ query from DB

        try:
            result = self.query_from_db(cfg)
        except ConfigurationNotInDBError:
            pass
        else:
            logger.info("DB Hit")
            return opentuner.Result(time=result)

        # }}}

        bound_transform = self.transform_func.bind_args(self.einsum,
                                                        **cfg)

        try:
            logger.info("\n"+stringify_comparison_vs_roofline(
                self.einsum,
                transform=bound_transform,
                cl_ctx=self.cl_ctx,
                long_dim_length=self.long_dim_length,
            ))
        except InvalidParameterError as err:
            logger.info(f"Ignored configuration due to '{err}'.")
            return opentuner.Result(time=np.inf)

        runtime = timeit(self.einsum,
                         cl_ctx=self.cl_ctx,
                         transform=bound_transform,
                         long_dim_length=self.long_dim_length
                         )
        self.record_into_db(runtime, cfg)

        return opentuner.Result(time=runtime)

# }}}


def autotune(einsum: FusedEinsum, module_path: str, cl_ctx: cl.Context,
             *,
             db_path: Optional[str] = None,
             long_dim_length: int = 100_000,
             stop_after: Optional[int] = None,
             ) -> None:
    """
    For a transform space specified in *module_path*, searches the parameter
    space and records the timing results for each run in *db_path*.

    :param stop_after: After these many trials the routine exits. Pass *None*
        to go on indefinitely.
    """
    if not os.path.isabs(module_path):
        raise ValueError("autotune expects an absolute path for the module")

    if db_path is None:
        db_path = DEFAULT_DB

    from argparse import Namespace  # :puke: but required by opentuner. Big brain.

    kwargs: Mapping[str, Any] = {
        "machine_class": None, "parallel_compile": False,
        "test_limit": None, "stop_after": stop_after, "parallelism": 4,
        "pipelining": 0, "bail_threshold": 100, "no_dups": True,
        "seed_configuration": [], "results_log": None,
        "results_log_details": None, "quiet": False, "display_frequency": 10,
        "technique": None, "list_techniques": False,
        "generate_bandit_technique": False, "label": None,
        "print_search_space_size": False, "database": None,
        "print_params": False
    }

    OpentunerTuner.main(args=Namespace(**kwargs),
                        einsum=einsum, cl_ctx=cl_ctx, module_path=module_path,
                        db_path=db_path,
                        long_dim_length=long_dim_length)

# vim: fdm=marker