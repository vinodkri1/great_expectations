import copy
import datetime
import inspect
import json
import logging
import uuid
from collections import OrderedDict
from functools import reduce, wraps
from io import StringIO
from typing import Any, Callable, Dict, Iterable, List, Tuple

import jsonschema
import numpy as np
import pandas as pd
from dateutil.parser import parse

from great_expectations.core.id_dict import IDDict
from great_expectations.data_asset import DataAsset
from great_expectations.data_asset.util import DocInherit, parse_result_format
from great_expectations.execution_environment.types import PathBatchSpec, S3BatchSpec
from great_expectations.validator.validator import Validator

from ..core.batch import Batch, BatchMarkers
from ..exceptions import BatchKwargsError, BatchSpecError, ValidationError
from ..expectations.registry import register_metric
from ..validator.validation_graph import MetricEdgeKey
from .execution_engine import ExecutionEngine

logger = logging.getLogger(__name__)

try:
    import pyspark.sql.functions as F
    import pyspark.sql.types as sparktypes
    from pyspark.ml.feature import Bucketizer
    from pyspark.sql import DataFrame, SQLContext, Window
    from pyspark.sql.functions import (
        array,
        col,
        count,
        countDistinct,
        datediff,
        desc,
        expr,
        isnan,
        lag,
    )
    from pyspark.sql.functions import length as length_
    from pyspark.sql.functions import (
        lit,
        monotonically_increasing_id,
        stddev_samp,
        udf,
        when,
        year,
    )
except ImportError as e:
    logger.debug(str(e))
    logger.debug(
        "Unable to load spark context; install optional spark dependency for support."
    )


class MetaSparkDFExecutionEngine(ExecutionEngine):
    """MetaSparkDFExecutionEngine is a thin layer between ExecutionEngine and SparkDFExecutionEngine.
    This two-layer inheritance is required to make @classmethod decorators work.
    Practically speaking, that means that MetaSparkDFExecutionEngine implements \
    expectation decorators, like `column_map_expectation` and `column_aggregate_expectation`, \
    and SparkDFExecutionEngine implements the expectation methods themselves.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @classmethod
    def column_map_expectation(cls, func):
        """Constructs an expectation using column-map semantics.


        The MetaSparkDFExecutionEngine implementation replaces the "column" parameter supplied by the user with a Spark Dataframe
        with the actual column data. The current approach for functions implementing expectation logic is to append
        a column named "__success" to this dataframe and return to this decorator.

        See :func:`column_map_expectation <great_expectations.ExecutionEngine.base.ExecutionEngine.column_map_expectation>` \
        for full documentation of this function.
        """
        argspec = inspect.getfullargspec(func)[0][1:]

        @Validator.expectation(argspec)
        @wraps(func)
        def inner_wrapper(
            self, column, mostly=None, result_format=None, *args, **kwargs,
        ):
            """
            This whole decorator is pending a re-write. Currently there is are huge performance issues
            when the # of unexpected elements gets large (10s of millions). Additionally, there is likely
            easy optimization opportunities by coupling result_format with how many different transformations
            are done on the execution_engine, as is done in sqlalchemy_execution_engine.
            """

            # Rename column so we only have to handle dot notation here
            eval_col = "__eval_col_" + column.replace(".", "__").replace("`", "_")
            spark_df = self.dataframe.withColumn(eval_col, col(column))

            if result_format is None:
                result_format = self.default_expectation_args["result_format"]

            result_format = parse_result_format(result_format)

            # this is a little dangerous: expectations that specify "COMPLETE" result format and have a very
            # large number of unexpected results could hang for a long time. we should either call this out in docs
            # or put a limit on it
            if result_format["result_format"] == "COMPLETE":
                unexpected_count_limit = None
            else:
                unexpected_count_limit = result_format["partial_unexpected_count"]

            col_df = spark_df.select(col(eval_col))  # pyspark.sql.DataFrame

            # a couple of tests indicate that caching here helps performance
            col_df.persist()
            element_count = self.get_row_count()

            # FIXME temporary fix for missing/ignored value
            if func.__name__ not in [
                "expect_column_values_to_not_be_null",
                "expect_column_values_to_be_null",
            ]:
                col_df = col_df.filter(col_df[0].isNotNull())
                # these nonnull_counts are cached by SparkDFExecutionEngine
                nonnull_count = self.get_column_nonnull_count(eval_col)
            else:
                nonnull_count = element_count

            # success_df will have columns [column, '__success']
            # this feels a little hacky, so might want to change
            success_df = func(self, col_df, *args, **kwargs)
            success_count = success_df.filter("__success = True").count()

            unexpected_count = nonnull_count - success_count

            if unexpected_count == 0:
                # save some computation time if no unexpected items
                maybe_limited_unexpected_list = []
            else:
                # here's an example of a place where we could do optimizations if we knew result format: see
                # comment block below
                unexpected_df = success_df.filter("__success = False")
                if unexpected_count_limit:
                    unexpected_df = unexpected_df.limit(unexpected_count_limit)
                maybe_limited_unexpected_list = [
                    row[eval_col] for row in unexpected_df.collect()
                ]

                if "output_strftime_format" in kwargs:
                    output_strftime_format = kwargs["output_strftime_format"]
                    parsed_maybe_limited_unexpected_list = []
                    for val in maybe_limited_unexpected_list:
                        if val is None:
                            parsed_maybe_limited_unexpected_list.append(val)
                        else:
                            if isinstance(val, str):
                                val = parse(val)
                            parsed_maybe_limited_unexpected_list.append(
                                datetime.datetime.strftime(val, output_strftime_format)
                            )
                    maybe_limited_unexpected_list = parsed_maybe_limited_unexpected_list

            success, percent_success = self._calc_map_expectation_success(
                success_count, nonnull_count, mostly
            )

            # Currently the abstraction of "result_format" that _format_column_map_output provides
            # limits some possible optimizations within the column-map decorator. It seems that either
            # this logic should be completely rolled into the processing done in the column_map decorator, or that the decorator
            # should do a minimal amount of computation agnostic of result_format, and then delegate the rest to this method.
            # In the first approach, it could make sense to put all of this decorator logic in ExecutionEngine, and then implement
            # properties that require execution_engine-type-dependent implementations (as is done with SparkDFExecutionEngine.row_count currently).
            # Then a new execution_engine type could just implement these properties/hooks and ExecutionEngine could deal with caching these and
            # with the optimizations based on result_format. A side benefit would be implementing an interface for the user
            # to get basic info about a execution_engine in a standardized way, e.g. my_execution_engine.row_count, my_execution_engine.columns (only for
            # tablular execution_engine maybe). However, unclear if this is worth it or if it would conflict with optimizations being done
            # in other execution_engine implementations.
            return_obj = self._format_map_output(
                result_format,
                success,
                element_count,
                nonnull_count,
                unexpected_count,
                maybe_limited_unexpected_list,
                unexpected_index_list=None,
            )

            # FIXME Temp fix for result format
            if func.__name__ in [
                "expect_column_values_to_not_be_null",
                "expect_column_values_to_be_null",
            ]:
                del return_obj["result"]["unexpected_percent_nonmissing"]
                del return_obj["result"]["missing_count"]
                del return_obj["result"]["missing_percent"]
                try:
                    del return_obj["result"]["partial_unexpected_counts"]
                except KeyError:
                    pass

            col_df.unpersist()

            return return_obj

        inner_wrapper.__name__ = func.__name__
        inner_wrapper.__doc__ = func.__doc__

        return inner_wrapper

    @classmethod
    def column_pair_map_expectation(cls, func):
        """
        The column_pair_map_expectation decorator handles boilerplate issues surrounding the common pattern of evaluating
        truthiness of some condition on a per row basis across a pair of columns.
        """
        argspec = inspect.getfullargspec(func)[0][1:]

        @Validator.expectation(argspec)
        @wraps(func)
        def inner_wrapper(
            self,
            column_A,
            column_B,
            mostly=None,
            ignore_row_if="both_values_are_missing",
            result_format=None,
            *args,
            **kwargs,
        ):
            # Rename column so we only have to handle dot notation here
            eval_col_A = "__eval_col_A_" + column_A.replace(".", "__").replace("`", "_")
            eval_col_B = "__eval_col_B_" + column_B.replace(".", "__").replace("`", "_")

            spark_df = self.dataframe.withColumn(eval_col_A, col(column_A)).withColumn(
                eval_col_B, col(column_B)
            )

            if result_format is None:
                result_format = self.default_expectation_args["result_format"]

            result_format = parse_result_format(result_format)

            # this is a little dangerous: expectations that specify "COMPLETE" result format and have a very
            # large number of unexpected results could hang for a long time. we should either call this out in docs
            # or put a limit on it
            if result_format["result_format"] == "COMPLETE":
                unexpected_count_limit = None
            else:
                unexpected_count_limit = result_format["partial_unexpected_count"]

            cols_df = spark_df.select(eval_col_A, eval_col_B).withColumn(
                "__row", monotonically_increasing_id()
            )  # pyspark.sql.DataFrame

            # a couple of tests indicate that caching here helps performance
            cols_df.cache()
            element_count = self.get_row_count()

            if ignore_row_if == "both_values_are_missing":
                boolean_mapped_null_values = cols_df.selectExpr(
                    "`__row`",
                    "`{0}` AS `A_{0}`".format(eval_col_A),
                    "`{0}` AS `B_{0}`".format(eval_col_B),
                    "ISNULL(`{}`) AND ISNULL(`{}`) AS `__null_val`".format(
                        eval_col_A, eval_col_B
                    ),
                )
            elif ignore_row_if == "either_value_is_missing":
                boolean_mapped_null_values = cols_df.selectExpr(
                    "`__row`",
                    "`{0}` AS `A_{0}`".format(eval_col_A),
                    "`{0}` AS `B_{0}`".format(eval_col_B),
                    "ISNULL(`{}`) OR ISNULL(`{}`) AS `__null_val`".format(
                        eval_col_A, eval_col_B
                    ),
                )
            elif ignore_row_if == "never":
                boolean_mapped_null_values = cols_df.selectExpr(
                    "`__row`",
                    "`{0}` AS `A_{0}`".format(eval_col_A),
                    "`{0}` AS `B_{0}`".format(eval_col_B),
                    lit(False).alias("__null_val"),
                )
            else:
                raise ValueError("Unknown value of ignore_row_if: %s", (ignore_row_if,))

            # since pyspark guaranteed each columns selected has the same number of rows, no need to do assert as in pandas
            # assert series_A.count() == (
            #     series_B.count()), "Series A and B must be the same length"

            nonnull_df = boolean_mapped_null_values.filter("__null_val = False")
            nonnull_count = nonnull_df.count()

            col_A_df = nonnull_df.select("__row", "`A_{}`".format(eval_col_A))
            col_B_df = nonnull_df.select("__row", "`B_{}`".format(eval_col_B))

            success_df = func(self, col_A_df, col_B_df, *args, **kwargs)
            success_count = success_df.filter("__success = True").count()

            unexpected_count = nonnull_count - success_count
            if unexpected_count == 0:
                # save some computation time if no unexpected items
                maybe_limited_unexpected_list = []
            else:
                # here's an example of a place where we could do optimizations if we knew result format: see
                # comment block below
                unexpected_df = success_df.filter("__success = False")
                if unexpected_count_limit:
                    unexpected_df = unexpected_df.limit(unexpected_count_limit)
                maybe_limited_unexpected_list = [
                    (row["A_{}".format(eval_col_A)], row["B_{}".format(eval_col_B)],)
                    for row in unexpected_df.collect()
                ]

                if "output_strftime_format" in kwargs:
                    output_strftime_format = kwargs["output_strftime_format"]
                    parsed_maybe_limited_unexpected_list = []
                    for val in maybe_limited_unexpected_list:
                        if val is None or (val[0] is None or val[1] is None):
                            parsed_maybe_limited_unexpected_list.append(val)
                        else:
                            if isinstance(val[0], str) and isinstance(val[1], str):
                                val = (parse(val[0]), parse(val[1]))
                            parsed_maybe_limited_unexpected_list.append(
                                (
                                    datetime.datetime.strftime(
                                        val[0], output_strftime_format
                                    ),
                                    datetime.datetime.strftime(
                                        val[1], output_strftime_format
                                    ),
                                )
                            )
                    maybe_limited_unexpected_list = parsed_maybe_limited_unexpected_list

            success, percent_success = self._calc_map_expectation_success(
                success_count, nonnull_count, mostly
            )

            # Currently the abstraction of "result_format" that _format_column_map_output provides
            # limits some possible optimizations within the column-map decorator. It seems that either
            # this logic should be completely rolled into the processing done in the column_map decorator, or that the decorator
            # should do a minimal amount of computation agnostic of result_format, and then delegate the rest to this method.
            # In the first approach, it could make sense to put all of this decorator logic in ExecutionEngine, and then implement
            # properties that require execution_engine-type-dependent implementations (as is done with SparkDFExecutionEngine.row_count currently).
            # Then a new execution_engine type could just implement these properties/hooks and ExecutionEngine could deal with caching these and
            # with the optimizations based on result_format. A side benefit would be implementing an interface for the user
            # to get basic info about a execution_engine in a standardized way, e.g. my_execution_engine.row_count, my_execution_engine.columns (only for
            # tablular execution_engine maybe). However, unclear if this is worth it or if it would conflict with optimizations being done
            # in other execution_engine implementations.
            return_obj = self._format_map_output(
                result_format,
                success,
                element_count,
                nonnull_count,
                unexpected_count,
                maybe_limited_unexpected_list,
                unexpected_index_list=None,
            )

            # # FIXME Temp fix for result format
            # if func.__name__ in ['expect_column_values_to_not_be_null', 'expect_column_values_to_be_null']:
            #     del return_obj['result']['unexpected_percent_nonmissing']
            #     del return_obj['result']['missing_count']
            #     del return_obj['result']['missing_percent']
            #     try:
            #         del return_obj['result']['partial_unexpected_counts']
            #     except KeyError:
            #         pass

            cols_df.unpersist()

            return return_obj

        inner_wrapper.__name__ = func.__name__
        inner_wrapper.__doc__ = func.__doc__

        return inner_wrapper

    @classmethod
    def multicolumn_map_expectation(cls, func):
        """
        The multicolumn_map_expectation decorator handles boilerplate issues surrounding the common pattern of
        evaluating truthiness of some condition on a per row basis across a set of columns.
        """
        argspec = inspect.getfullargspec(func)[0][1:]

        @Validator.expectation(argspec)
        @wraps(func)
        def inner_wrapper(
            self,
            column_list,
            mostly=None,
            ignore_row_if="all_values_are_missing",
            result_format=None,
            *args,
            **kwargs,
        ):
            spark_df = self.dataframe
            # Rename column so we only have to handle dot notation here
            eval_cols = []
            for col_name in column_list:
                eval_col = "__eval_col_" + col_name.replace(".", "__").replace("`", "_")
                eval_cols.append(eval_col)
                spark_df = self.dataframe.withColumn(eval_col, col(col_name))
            if result_format is None:
                result_format = self.default_expectation_args["result_format"]

            result_format = parse_result_format(result_format)

            # this is a little dangerous: expectations that specify "COMPLETE" result format and have a very
            # large number of unexpected results could hang for a long time. we should either call this out in docs
            # or put a limit on it
            if result_format["result_format"] == "COMPLETE":
                unexpected_count_limit = None
            else:
                unexpected_count_limit = result_format["partial_unexpected_count"]

            temp_df = spark_df.select(*eval_cols)  # pyspark.sql.DataFrame

            # a couple of tests indicate that caching here helps performance
            temp_df.cache()
            element_count = self.get_row_count()

            if ignore_row_if == "all_values_are_missing":
                boolean_mapped_skip_values = temp_df.select(
                    [
                        *eval_cols,
                        reduce(
                            lambda a, b: a & b, [col(c).isNull() for c in eval_cols]
                        ).alias("__null_val"),
                    ]
                )
            elif ignore_row_if == "any_value_is_missing":
                boolean_mapped_skip_values = temp_df.select(
                    [
                        *eval_cols,
                        reduce(
                            lambda a, b: a | b, [col(c).isNull() for c in eval_cols]
                        ).alias("__null_val"),
                    ]
                )
            elif ignore_row_if == "never":
                boolean_mapped_skip_values = temp_df.select(
                    [*eval_cols, lit(False).alias("__null_val")]
                )
            else:
                raise ValueError("Unknown value of ignore_row_if: %s", (ignore_row_if,))

            nonnull_df = boolean_mapped_skip_values.filter("__null_val = False")
            nonnull_count = nonnull_df.count()

            cols_df = nonnull_df.select(*eval_cols)

            success_df = func(self, cols_df, *args, **kwargs)
            success_count = success_df.filter("__success = True").count()

            unexpected_count = nonnull_count - success_count
            if unexpected_count == 0:
                maybe_limited_unexpected_list = []
            else:
                # here's an example of a place where we could do optimizations if we knew result format: see
                # comment block below
                unexpected_df = success_df.filter("__success = False")
                if unexpected_count_limit:
                    unexpected_df = unexpected_df.limit(unexpected_count_limit)
                maybe_limited_unexpected_list = [
                    OrderedDict(
                        (col_name, row[eval_col_name])
                        for (col_name, eval_col_name) in zip(column_list, eval_cols)
                    )
                    for row in unexpected_df.collect()
                ]

                if "output_strftime_format" in kwargs:
                    output_strftime_format = kwargs["output_strftime_format"]
                    parsed_maybe_limited_unexpected_list = []
                    for val in maybe_limited_unexpected_list:
                        if val is None or not all(v for k, v in val):
                            parsed_maybe_limited_unexpected_list.append(val)
                        else:
                            if all(isinstance(v, str) for k, v in val):
                                val = OrderedDict((k, parse(v)) for k, v in val)
                            parsed_maybe_limited_unexpected_list.append(
                                OrderedDict(
                                    (
                                        k,
                                        datetime.datetime.strftime(
                                            v, output_strftime_format
                                        ),
                                    )
                                    for k, v in val
                                )
                            )
                    maybe_limited_unexpected_list = parsed_maybe_limited_unexpected_list

            success, percent_success = self._calc_map_expectation_success(
                success_count, nonnull_count, mostly
            )

            # Currently the abstraction of "result_format" that _format_column_map_output provides
            # limits some possible optimizations within the column-map decorator. It seems that either
            # this logic should be completely rolled into the processing done in the column_map decorator, or that the decorator
            # should do a minimal amount of computation agnostic of result_format, and then delegate the rest to this method.
            # In the first approach, it could make sense to put all of this decorator logic in ExecutionEngine, and then implement
            # properties that require execution_engine-type-dependent implementations (as is done with SparkDFExecutionEngine.row_count currently).
            # Then a new execution_engine type could just implement these properties/hooks and ExecutionEngine could deal with caching these and
            # with the optimizations based on result_format. A side benefit would be implementing an interface for the user
            # to get basic info about a execution_engine in a standardized way, e.g. my_execution_engine.row_count, my_execution_engine.columns (only for
            # tablular execution_engine maybe). However, unclear if this is worth it or if it would conflict with optimizations being done
            # in other execution_engine implementations.
            return_obj = self._format_map_output(
                result_format,
                success,
                element_count,
                nonnull_count,
                unexpected_count,
                maybe_limited_unexpected_list,
                unexpected_index_list=None,
            )

            temp_df.unpersist()

            return return_obj

        inner_wrapper.__name__ = func.__name__
        inner_wrapper.__doc__ = func.__doc__

        return inner_wrapper


class SparkDFExecutionEngine(MetaSparkDFExecutionEngine):
    """
This class holds an attribute `spark_df` which is a spark.sql.DataFrame.

--ge-feature-maturity-info--

    id: validation_engine_pyspark_self_managed
    title: Validation Engine - pyspark - Self-Managed
    icon:
    short_description: Use Spark DataFrame to validate data
    description: Use Spark DataFrame to validate data
    how_to_guide_url: https://docs.greatexpectations.io/en/latest/how_to_guides/creating_batches/how_to_load_a_spark_dataframe_as_a_batch.html
    maturity: Production
    maturity_details:
        api_stability: Stable
        implementation_completeness: Moderate
        unit_test_coverage: Complete
        integration_infrastructure_test_coverage: N/A -> see relevant Datasource evaluation
        documentation_completeness: Complete
        bug_risk: Low/Moderate
        expectation_completeness: Moderate

    id: validation_engine_databricks
    title: Validation Engine - Databricks
    icon:
    short_description: Use Spark DataFrame in a Databricks cluster to validate data
    description: Use Spark DataFrame in a Databricks cluster to validate data
    how_to_guide_url: https://docs.greatexpectations.io/en/latest/how_to_guides/creating_batches/how_to_load_a_spark_dataframe_as_a_batch.html
    maturity: Beta
    maturity_details:
        api_stability: Stable
        implementation_completeness: Low (dbfs-specific handling)
        unit_test_coverage: N/A -> implementation not different
        integration_infrastructure_test_coverage: Minimal (we've tested a bit, know others have used it)
        documentation_completeness: Moderate (need docs on managing project configuration via dbfs/etc.)
        bug_risk: Low/Moderate
        expectation_completeness: Moderate

    id: validation_engine_emr_spark
    title: Validation Engine - EMR - Spark
    icon:
    short_description: Use Spark DataFrame in an EMR cluster to validate data
    description: Use Spark DataFrame in an EMR cluster to validate data
    how_to_guide_url: https://docs.greatexpectations.io/en/latest/how_to_guides/creating_batches/how_to_load_a_spark_dataframe_as_a_batch.html
    maturity: Experimental
    maturity_details:
        api_stability: Stable
        implementation_completeness: Low (need to provide guidance on "known good" paths, and we know there are many "knobs" to tune that we have not explored/tested)
        unit_test_coverage: N/A -> implementation not different
        integration_infrastructure_test_coverage: Unknown
        documentation_completeness: Low (must install specific/latest version but do not have docs to that effect or of known useful paths)
        bug_risk: Low/Moderate
        expectation_completeness: Moderate

    id: validation_engine_spark_other
    title: Validation Engine - Spark - Other
    icon:
    short_description: Use Spark DataFrame to validate data
    description: Use Spark DataFrame to validate data
    how_to_guide_url: https://docs.greatexpectations.io/en/latest/how_to_guides/creating_batches/how_to_load_a_spark_dataframe_as_a_batch.html
    maturity: Experimental
    maturity_details:
        api_stability: Stable
        implementation_completeness: Other (we haven't tested possibility, known glue deployment)
        unit_test_coverage: N/A -> implementation not different
        integration_infrastructure_test_coverage: Unknown
        documentation_completeness: Low (must install specific/latest version but do not have docs to that effect or of known useful paths)
        bug_risk: Low/Moderate
        expectation_completeness: Moderate

--ge-feature-maturity-info--
    """

    recognized_batch_definition_keys = {"limit"}

    recognized_batch_spec_defaults = {
        "reader_method",
        "reader_options",
    }

    def __init__(self, *args, **kwargs):
        # Creation of the Spark DataFrame is done outside this class
        self._persist = kwargs.pop("persist", True)
        super().__init__(*args, **kwargs)

    def load_batch(
        self, batch_definition=None, batch_spec=None, in_memory_dataset=None
    ) -> Batch:
        # We need to build a batch_markers to be used in the dataframe
        if batch_spec and batch_definition:
            #### IS THIS OK?
            assert isinstance(batch_spec, IDDict)
        elif batch_spec and not batch_definition:
            logger.info("loading a batch without a batch_definition")
            batch_definition = {}
        else:
            execution_environment_name = batch_definition.get("execution_environment")
            if not self._data_context:
                raise ValueError("Cannot use a batch definition without a data context")
            execution_environment = self._data_context.get_execution_environment(
                execution_environment_name
            )
            data_connector_name = batch_definition.get("data_connector")
            assert data_connector_name, "Batch definition must specify a data_connector"

            data_connector = execution_environment.get_data_connector(
                data_connector_name
            )
            batch_spec = data_connector.build_batch_spec(
                batch_definition=batch_definition
            )

        # We need to build a batch_markers to be used in the dataframe
        batch_markers = BatchMarkers(
            {
                "ge_load_time": datetime.datetime.now(datetime.timezone.utc).strftime(
                    "%Y%m%dT%H%M%S.%fZ"
                )
            }
        )

        batch_id = batch_spec.to_id()

        if in_memory_dataset is not None:
            if batch_definition.get("data_asset_name") and batch_definition.get(
                "partition_name"
            ):
                df = in_memory_dataset
            else:
                raise ValueError(
                    "To pass an in_memory_dataset, you must also pass a data_asset_name and partition_id"
                )
        else:
            if data_connector.get_config().get("class_name") == "DataConnector":
                raise ValueError(
                    "No in_memory_dataset found. To use a data_connector with class DataConnector, please ensure that "
                    "you are passing a dataset to load_batch()"
                )

            # We will use and manipulate reader_options along the way
            reader_options = batch_spec.get("reader_options", {})

            if isinstance(batch_spec, PathBatchSpec):
                path = batch_spec["path"]
                reader_method = batch_spec.get("reader_method")
                reader_fn = self._get_reader_fn(reader_method, path)
                df = reader_fn(path, **reader_options)
            elif isinstance(batch_spec, S3BatchSpec):
                url, s3_object = data_connector.get_s3_object(batch_spec=batch_spec)
                reader_method = batch_spec.get("reader_method")
                reader_fn = self._get_reader_fn(reader_method, url.key)
                df = reader_fn(
                    StringIO(
                        s3_object["Body"]
                        .read()
                        .decode(s3_object.get("ContentEncoding", "utf-8"))
                    ),
                    **reader_options,
                )
            else:
                raise BatchSpecError(
                    "Invalid batch_spec: path, s3, or df is required for a PandasDatasource",
                    batch_spec,
                )

        limit = batch_definition.get("limit") or batch_spec.get("limit")
        if limit:
            df = df.limit(limit)

        if self._persist:
            df.persist()

        if not self.batches.get(batch_id):
            batch = Batch(
                execution_engine=self,
                batch_spec=batch_spec,
                data=df,
                batch_definition=batch_definition,
                batch_markers=batch_markers,
                data_context=self._data_context,
            )
            self.batches[batch_id] = batch
        else:
            batch = self.batches.get(batch_id)

        self._loaded_batch_id = batch_id
        return batch

    @property
    def dataframe(self):
        """If a batch has been loaded, returns a Spark Dataframe containing the data within the loaded batch"""
        if not self.loaded_batch:
            raise ValueError(
                "Batch has not been loaded - please run load_batch() to load a batch."
            )

        return self.loaded_batch.data

    @staticmethod
    def guess_reader_method_from_path(path):
        """Based on a given filepath, decides a reader method. Currently supports tsv, csv, and parquet. If none of these
        file extensions are used, returns BatchKwargsError stating that it is unable to determine the current path.

        Args:
            path - A given file path

        Returns:
            A dictionary entry of format {'reader_method': reader_method}

        """
        if path.endswith(".csv") or path.endswith(".tsv"):
            return {"reader_method": "csv"}
        elif path.endswith(".parquet"):
            return {"reader_method": "parquet"}

        raise BatchKwargsError(
            "Unable to determine reader method from path: %s" % path, {"path": path}
        )

    def _get_reader_fn(self, reader, reader_method=None, path=None):
        """Static helper for providing reader_fn

        Args:
            reader: the base spark reader to use; this should have had reader_options applied already
            reader_method: the name of the reader_method to use, if specified
            path (str): the path to use to guess reader_method if it was not specified

        Returns:
            ReaderMethod to use for the filepath

        """
        if reader_method is None and path is None:
            raise BatchKwargsError(
                "Unable to determine spark reader function without reader_method or path.",
                {"reader_method": reader_method},
            )

        if reader_method is None:
            reader_method = self.guess_reader_method_from_path(path=path)[
                "reader_method"
            ]

        try:
            if reader_method.lower() == "delta":
                return reader.format("delta").load

            return getattr(reader, reader_method)
        except AttributeError:
            raise BatchKwargsError(
                "Unable to find reader_method %s in spark." % reader_method,
                {"reader_method": reader_method},
            )

    def process_batch_definition(self, batch_definition, batch_spec):
        """Given that the batch definition has a limit state, transfers the limit dictionary entry from the batch_definition
        to the batch_spec.

                Args:
                    batch_definition: The batch definition to use in configuring the batch spec's limit
                    batch_spec: a batch_spec dictionary whose limit needs to be configured

                Returns:
                    ReaderMethod to use for the filepath

                """
        limit = batch_definition.get("limit")
        if limit is not None:
            if not batch_spec.get("limit"):
                batch_spec["limit"] = limit
        return batch_spec

    def get_domain_dataframe(
        self,
        domain_kwargs: dict,
        batches: Dict[str, Batch] = None,
        filter_column_isnull=False,
    ) -> "pyspark.sql.DataFrame":
        """Uses a given batch dictionary and domain kwargs (which include a row condition and a condition parser)
        to obtain and/or query a batch. Returns in the format of a Pandas Series if only a single column is desired,
        or otherwise a Data Frame.

        Args:
            domain_kwargs (dict) - A dictionary consisting of the domain kwargs specifying which data to obtain
            batches (dict) - A dictionary specifying batch id and which batches to obtain

        Returns:
            One of 2 formats, as specified by the domain kwargs. Either a Column (Pandas Series) or otherwise a Pandas
            Data Frame.
        """
        batch_id = domain_kwargs.get("batch_id")
        if batch_id is None:
            # We allow no batch id specified if there is only one batch
            if batches and len(batches) == 1:
                batch = [batch for batch in batches.values()][0]
            elif self.loaded_batch:
                batch = self.loaded_batch
            else:
                raise ValidationError(
                    "No batch is specified, but multiple batches are available."
                )
        else:
            if batches and batch_id in batches:
                batch = batches[batch_id]
            elif batch_id == self.loaded_batch_id:
                batch = self.loaded_batch
            else:
                raise ValidationError(f"Unable to find batch with batch_id {batch_id}")

        table = domain_kwargs.get("table", None)
        if table:
            raise ValueError(
                "SparkExecutionEngine does not currently support multiple named tables."
            )

        row_condition = domain_kwargs.get("row_condition", None)
        if row_condition:
            condition_parser = domain_kwargs.get("condition_parser", None)
            if condition_parser not in ["spark"]:
                raise ValueError(
                    "condition_parser is required when setting a row_condition,"
                    " and must be 'spark'"
                )
            else:
                data = batch.data.filter(row_condition)
        else:
            data = batch.data

        column = domain_kwargs.get("column", None)
        if column:
            # Rename column so we only have to handle dot notation here
            # TODO: verify comprehensive nested column support
            # eval_column = self._get_eval_column_name(column)
            eval_column = column
            data = data.withColumn(eval_column, F.col(column))
            if filter_column_isnull:
                data = data.filter(F.col(eval_column).isNotNull())
        return data

    def _get_eval_column_name(self, column):
        """Given the name of a column (string), returns the name of the corresponding eval column"""
        return "__eval_col_" + column.replace(".", "__").replace("`", "_")

    def _column_map_count(
        self,
        metric_name: str,
        batches: Dict[str, Batch],
        execution_engine: "SparkDFExecutionEngine",
        metric_domain_kwargs: dict,
        metric_value_kwargs: dict,
        metrics: Dict[Tuple, Any],
        filter_column_isnull,
        **kwargs,
    ):
        """Return the count of nonzero values from the map-style metric in the metrics dictionary"""
        assert metric_name.endswith(".count")
        metric_key = MetricEdgeKey(
            metric_name[: -len(".count")],
            metric_domain_kwargs,
            metric_value_kwargs,
            filter_column_isnull=filter_column_isnull,
        ).id
        domain_kwargs = {
            k: v for (k, v) in metric_domain_kwargs.items() if k != "column"
        }
        return metrics.get(metric_key), domain_kwargs

    def _column_map_values(
        self,
        metric_name: str,
        batches: Dict[str, Batch],
        execution_engine: "SparkDFExecutionEngine",
        metric_domain_kwargs: dict,
        metric_value_kwargs: dict,
        metrics: Dict[Tuple, Any],
        filter_column_isnull,
        **kwargs,
    ):
        """Return values from the specified domain that match the map-style metric in the metrics dictionary."""
        assert metric_name.endswith(".unexpected_values")
        # column_map_values adds "result_format" as a value_kwarg to its underlying metric; get and remove it
        result_format = metric_value_kwargs["result_format"]
        base_metric_value_kwargs = {
            k: v for k, v in metric_value_kwargs.items() if k != "result_format"
        }
        metric_key = MetricEdgeKey(
            metric_name[: -len(".unexpected_values")],
            metric_domain_kwargs,
            base_metric_value_kwargs,
            filter_column_isnull=filter_column_isnull,
        ).id
        condition = metrics.get(metric_key)
        column = metric_domain_kwargs["column"]
        # column = self._get_eval_column_name(metric_domain_kwargs["column"])
        filtered = self.get_domain_dataframe(metric_domain_kwargs).filter(~condition)
        if result_format["result_format"] == "COMPLETE":
            return list(filtered.select(F.col(column)).collect())
        else:
            return list(
                filtered.select(F.col(column))
                .limit(result_format["partial_unexpected_count"])
                .collect()
            )

    def _column_map_value_counts(
        self,
        metric_name: str,
        batches: Dict[str, Batch],
        execution_engine: "SparkDFExecutionEngine",
        metric_domain_kwargs: dict,
        metric_value_kwargs: dict,
        metrics: Dict[Tuple, Any],
        filter_column_isnull,
        **kwargs,
    ):
        """Returns all unique values in the column and their corresponding counts"""
        assert metric_name.endswith(".unexpected_value_counts")
        # column_map_values adds "result_format" as a value_kwarg to its underlying metric; get and remove it
        result_format = metric_value_kwargs["result_format"]
        base_metric_value_kwargs = {
            k: v for k, v in metric_value_kwargs.items() if k != "result_format"
        }
        metric_key = MetricEdgeKey(
            metric_name[: -len(".unexpected_value_counts")],
            metric_domain_kwargs,
            base_metric_value_kwargs,
            filter_column_isnull=filter_column_isnull,
        ).id
        condition = metrics.get(metric_key)
        column = metric_domain_kwargs["column"]
        # column = self._get_eval_column_name(metric_domain_kwargs["column"])
        filtered = self.get_domain_dataframe(metric_domain_kwargs).filter(condition)
        value_counts = filtered.groupBy(F.col(column)).count()
        if result_format["result_format"] == "COMPLETE":
            return value_counts
        else:
            return value_counts[result_format["partial_unexpected_count"]]

    def _column_map_rows(
        self,
        metric_name: str,
        batches: Dict[str, Batch],
        execution_engine: "PandasExecutionEngine",
        metric_domain_kwargs: dict,
        metric_value_kwargs: dict,
        metrics: Dict[Tuple, Any],
        filter_column_isnull,
        **kwargs,
    ):
        """Return values from the specified domain (ignoring the column constraint) that match the map-style metric in the metrics dictionary."""
        row_domain = {k: v for (k, v) in metric_domain_kwargs.items() if k != "column"}
        data = execution_engine.get_domain_dataframe(
            row_domain, batches, filter_column_isnull=filter_column_isnull
        )
        assert metric_name.endswith(".unexpected_rows")
        # column_map_values adds "result_format" as a value_kwarg to its underlying metric; get and remove it
        result_format = metric_value_kwargs["result_format"]
        base_metric_value_kwargs = {
            k: v for k, v in metric_value_kwargs.items() if k != "result_format"
        }
        metric_key = MetricEdgeKey(
            metric_name[: -len(".unexpected_rows")],
            metric_domain_kwargs,
            base_metric_value_kwargs,
            filter_column_isnull=filter_column_isnull,
        ).id
        condition = metrics.get(metric_key)
        filtered = self.get_domain_dataframe(metric_domain_kwargs).filter(condition)
        if result_format["result_format"] == "COMPLETE":
            return filtered.collect()
        else:
            return filtered.limit(result_format["partial_unexpected_count"]).collect()

    @classmethod
    def column_map_metric(
        cls,
        metric_name: str,
        metric_domain_keys: Tuple[str, ...],
        metric_value_keys: Tuple[str, ...],
        metric_dependencies: Tuple[str, ...],
        filter_column_isnull: bool = True,
    ):
        """
        A decorator for declaring a metric provider
        """

        def outer(metric_fn: Callable):
            _declared_name = metric_name

            @wraps(metric_fn)
            def inner_func(
                self,
                metric_name: str,
                batches: Dict[str, Batch],
                execution_engine: SparkDFExecutionEngine,
                metric_domain_kwargs: dict,
                metric_value_kwargs: dict,
                metrics: Dict[Tuple, Any],
                **kwargs,
            ):
                if _declared_name != metric_name:
                    logger.warning("using metric provider with an unrecognized metric")
                data = execution_engine.get_domain_dataframe(
                    metric_domain_kwargs, batches, filter_column_isnull
                )
                column = metric_domain_kwargs["column"]
                # eval_col = self._get_eval_column_name(column)
                eval_col = column

                metric_condition = metric_fn(
                    self,
                    column=F.col(eval_col),
                    metrics=metrics,
                    metric_domain_kwargs=metric_domain_kwargs,
                    metric_value_kwargs=metric_value_kwargs,
                    **kwargs,
                )
                if filter_column_isnull:
                    expected_condition = F.col(eval_col).isNotNull() & metric_condition
                else:
                    expected_condition = metric_condition
                return expected_condition

            register_metric(
                metric_name=metric_name,
                metric_domain_keys=metric_domain_keys,
                metric_value_keys=metric_value_keys,
                execution_engine=cls,
                metric_dependencies=tuple(),
                metric_provider=inner_func,
                bundle_computation=False,
                filter_column_isnull=filter_column_isnull,
            )
            register_metric(
                metric_name=metric_name + ".count",
                metric_domain_keys=metric_domain_keys,
                metric_value_keys=metric_value_keys,
                execution_engine=cls,
                metric_dependencies=(metric_name,),
                metric_provider=cls._column_map_count,
                bundle_computation=True,
                filter_column_isnull=filter_column_isnull,
            )
            # noinspection PyTypeChecker
            register_metric(
                metric_name=metric_name + ".unexpected_values",
                metric_domain_keys=metric_domain_keys,
                metric_value_keys=(*metric_value_keys, "result_format"),
                execution_engine=cls,
                metric_dependencies=(metric_name,),
                metric_provider=cls._column_map_values,
                bundle_computation=False,
                filter_column_isnull=filter_column_isnull,
            )
            # noinspection PyTypeChecker
            register_metric(
                metric_name=metric_name + ".unexpected_value_counts",
                metric_domain_keys=metric_domain_keys,
                metric_value_keys=(*metric_value_keys, "result_format"),
                execution_engine=cls,
                metric_dependencies=(metric_name,),
                metric_provider=cls._column_map_value_counts,
                bundle_computation=False,
                filter_column_isnull=filter_column_isnull,
            )
            # noinspection PyTypeChecker
            register_metric(
                metric_name=metric_name + ".unexpected_rows",
                metric_domain_keys=metric_domain_keys,
                metric_value_keys=(*metric_value_keys, "result_format"),
                execution_engine=cls,
                metric_dependencies=(metric_name,),
                metric_provider=cls._column_map_rows,
                bundle_computation=False,
                filter_column_isnull=filter_column_isnull,
            )
            return inner_func

        return outer

    def batch_resolve(
        self,
        resolve_batch: Iterable[Tuple[MetricEdgeKey, Callable, dict]],
        metrics: Dict[Tuple, Any] = None,
    ) -> dict:
        """For each metric name in the given resolve_batch, finds the domain of the metric and calculates it using a
        metric function from the given provider class.

                Args:
                    resolve_batch - A batch containing MetricEdgeKeys and their corresponding functions
                    metrics (dict) - A dictionary containing metrics and corresponding parameters

                Returns:
                    A dictionary of the collected metrics over their respective domains
                """
        if metrics is None:
            metrics = dict()

        aggregates: Dict[Tuple, dict] = dict()
        for metric_to_resolve, metric_provider, metric_provider_kwargs in resolve_batch:
            assert (
                metric_provider._can_be_bundled
            ), "batch_resolve only supports metrics that support bundled computation"
            # batch_id and table are the only determining factors for bundled metrics
            column_condition, domain_kwargs = metric_provider(
                self, **metric_provider_kwargs, metrics=metrics
            )
            if not isinstance(domain_kwargs, IDDict):
                domain_kwargs = IDDict(domain_kwargs)
            domain_id = domain_kwargs.to_id()
            if domain_id not in aggregates:
                aggregates[domain_id] = {
                    "column_conditions": [],
                    "ids": [],
                    "domain_kwargs": domain_kwargs,
                }
            aggregates[domain_id]["column_conditions"].append(column_condition)
            aggregates[domain_id]["ids"].append(metric_to_resolve.id)
        for aggregate in aggregates.values():
            df = self.get_domain_dataframe(aggregate["domain_kwargs"])
            assert len(aggregate["column_conditions"]) == len(aggregate["ids"])
            condition_ids = []
            sums = []
            for idx in range(len(aggregate["column_conditions"])):
                column_condition = aggregate["column_conditions"][idx]
                condition_id = str(uuid.uuid4())
                df = df.withColumn(
                    condition_id, F.when(column_condition, 1).otherwise(0)
                )
                condition_ids.append(condition_id)
                sums.append(F.sum(condition_id))

            res = df.agg(*sums).collect()
            assert (
                len(res) == 1
            ), "all bundle-computed metrics must be single-value statistics"
            assert len(aggregate["ids"]) == len(
                res[0]
            ), "unexpected number of metrics returned"
            for idx, id in enumerate(aggregate["ids"]):
                metrics[id] = res[0][idx]

        return metrics

    def head(self, n=5):
        """Returns dataframe head. Default is 5"""
        return self.dataframe.limit(n).toPandas()

    def get_row_count(self):
        """Returns the number of rows in a Dataframe"""
        return self.dataframe.count()

    def get_column_count(self):
        """Returns the number of columns in a column"""
        return len(self.dataframe.columns)

    def get_table_columns(self) -> List[str]:
        """Returns the columns in a Dataframe"""
        return self.dataframe.columns

    def get_column_nonnull_count(self, column):
        """Returns the number of nonnull values in a column"""
        return self.dataframe.filter(col(column).isNotNull()).count()

    def get_column_mean(self, column):
        """Returns the mean of a column"""
        # TODO need to apply this logic to other such methods?
        types = dict(self.dataframe.dtypes)
        if types[column] not in ("int", "float", "double", "bigint"):
            raise TypeError("Expected numeric column type for function mean()")
        result = self.dataframe.select(column).groupBy().mean().collect()[0]
        return result[0] if len(result) > 0 else None

    def get_column_sum(self, column):
        """Returns the sum of a column"""
        return self.dataframe.select(column).groupBy().sum().collect()[0][0]

    # TODO: consider getting all basic statistics in one go:
    def _describe_column(self, column):
        # temp_column = self.dataframe.select(column).where(col(column).isNotNull())
        # return self.dataframe.select(
        #     [
        #         count(temp_column),
        #         mean(temp_column),
        #         stddev(temp_column),
        #         min(temp_column),
        #         max(temp_column)
        #     ]
        # )
        pass

    def get_column_max(self, column, parse_strings_as_datetimes=False):
        """Returns the maximum value in a column"""
        temp_column = self.dataframe.select(column).where(col(column).isNotNull())
        if parse_strings_as_datetimes:
            temp_column = self._apply_dateutil_parse(temp_column)
        result = temp_column.agg({column: "max"}).collect()
        if not result or not result[0]:
            return None
        return result[0][0]

    def get_column_min(self, column, parse_strings_as_datetimes=False):
        """Returns the minimum value in a column"""
        temp_column = self.dataframe.select(column).where(col(column).isNotNull())
        if parse_strings_as_datetimes:
            temp_column = self._apply_dateutil_parse(temp_column)
        result = temp_column.agg({column: "min"}).collect()
        if not result or not result[0]:
            return None
        return result[0][0]

    def get_column_value_counts(self, column, sort="value", collate=None):
        """Returns column distinct values and corresponding counts"""
        if sort not in ["value", "count", "none"]:
            raise ValueError("sort must be either 'value', 'count', or 'none'")
        if collate is not None:
            raise ValueError(
                "collate parameter is not supported in SparkDFExecutionEngine"
            )
        value_counts = (
            self.dataframe.select(column)
            .where(col(column).isNotNull())
            .groupBy(column)
            .count()
        )
        if sort == "value":
            value_counts = value_counts.orderBy(column)
        elif sort == "count":
            value_counts = value_counts.orderBy(desc("count"))
        value_counts = value_counts.collect()
        series = pd.Series(
            [row["count"] for row in value_counts],
            index=pd.Index(data=[row[column] for row in value_counts], name="value"),
            name="count",
        )
        return series

    def get_column_unique_count(self, column):
        """Returns all unique column values"""
        return self.dataframe.agg(countDistinct(column)).collect()[0][0]

    def get_column_modes(self, column):
        """leverages computation done in _get_column_value_counts"""
        s = self.get_column_value_counts(column)
        return list(s[s == s.max()].index)

    def get_column_median(self, column):
        """Getter function for the column median"""
        # We will get the two middle values by choosing an epsilon to add
        # to the 50th percentile such that we always get exactly the middle two values
        # (i.e. 0 < epsilon < 1 / (2 * values))

        # Note that this can be an expensive computation; we are not exposing
        # spark's ability to estimate.
        # We add two to 2 * n_values to maintain a legitimate quantile
        # in the degnerate case when n_values = 0
        result = self.dataframe.approxQuantile(
            column, [0.5, 0.5 + (1 / (2 + (2 * self.get_row_count())))], 0
        )
        return np.mean(result)

    def get_column_quantiles(self, column, quantiles, allow_relative_error=False):
        """Given a set of quantile values, returns their corresponding approximations"""
        if allow_relative_error is False:
            allow_relative_error = 0.0
        if (
            not isinstance(allow_relative_error, float)
            or allow_relative_error < 0
            or allow_relative_error > 1
        ):
            raise ValueError(
                "SparkDFExecutionEngine requires relative error to be False or to be a float between 0 and 1."
            )
        return self.dataframe.approxQuantile(
            column, list(quantiles), allow_relative_error
        )

    def get_column_stdev(self, column):
        """Returns the standard deviation of a column"""
        return self.dataframe.select(stddev_samp(col(column))).collect()[0][0]

    def get_column_hist(self, column, bins):
        """return a list of counts corresponding to bins"""
        bins = list(
            copy.deepcopy(bins)
        )  # take a copy since we are inserting and popping
        if bins[0] == -np.inf or bins[0] == -float("inf"):
            added_min = False
            bins[0] = -float("inf")
        else:
            added_min = True
            bins.insert(0, -float("inf"))

        if bins[-1] == np.inf or bins[-1] == float("inf"):
            added_max = False
            bins[-1] = float("inf")
        else:
            added_max = True
            bins.append(float("inf"))

        temp_column = self.dataframe.select(column).where(col(column).isNotNull())
        bucketizer = Bucketizer(splits=bins, inputCol=column, outputCol="buckets")
        bucketed = bucketizer.setHandleInvalid("skip").transform(temp_column)

        # This is painful to do, but: bucketizer cannot handle values outside of a range
        # (hence adding -/+ infinity above)

        # Further, it *always* follows the numpy convention of lower_bound <= bin < upper_bound
        # for all but the last bin

        # But, since the last bin in our case will often be +infinity, we need to
        # find the number of values exactly equal to the upper bound to add those

        # We'll try for an optimization by asking for it at the same time
        if added_max:
            upper_bound_count = (
                temp_column.select(column).filter(col(column) == bins[-2]).count()
            )
        else:
            upper_bound_count = 0

        hist_rows = bucketed.groupBy("buckets").count().collect()
        # Spark only returns buckets that have nonzero counts.
        hist = [0] * (len(bins) - 1)
        for row in hist_rows:
            hist[int(row["buckets"])] = row["count"]

        hist[-2] += upper_bound_count

        if added_min:
            below_bins = hist.pop(0)
            bins.pop(0)
            if below_bins > 0:
                logger.warning("Discarding histogram values below lowest bin.")

        if added_max:
            above_bins = hist.pop(-1)
            bins.pop(-1)
            if above_bins > 0:
                logger.warning("Discarding histogram values above highest bin.")

        return hist

    def get_column_count_in_range(
        self, column, min_val=None, max_val=None, strict_min=False, strict_max=True
    ):
        """Returns the number of values in a column that exist within a certain range"""
        if min_val is None and max_val is None:
            raise ValueError("Must specify either min or max value")
        if min_val is not None and max_val is not None and min_val > max_val:
            raise ValueError("Min value must be <= to max value")

        result = self.dataframe.select(column)
        if min_val is not None:
            if strict_min:
                result = result.filter(col(column) > min_val)
            else:
                result = result.filter(col(column) >= min_val)
        if max_val is not None:
            if strict_max:
                result = result.filter(col(column) < max_val)
            else:
                result = result.filter(col(column) <= max_val)
        return result.count()

    # Utils
    @staticmethod
    def _apply_dateutil_parse(column):
        """Turns column values into dateutil format"""
        assert len(column.columns) == 1, "Expected DataFrame with 1 column"
        col_name = column.columns[0]
        _udf = udf(parse, sparktypes.TimestampType())
        return column.withColumn(col_name, _udf(col_name))

    # Expectations
    @DocInherit
    @MetaSparkDFExecutionEngine.column_map_expectation
    def expect_column_values_to_be_in_set(
        self,
        column,  # pyspark.sql.DataFrame
        value_set,  # List[Any]
        mostly=None,
        parse_strings_as_datetimes=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        if value_set is None:
            # vacuously true
            return column.withColumn("__success", lit(True))
        if parse_strings_as_datetimes:
            column = self._apply_dateutil_parse(column)
            value_set = [
                parse(value) if isinstance(value, str) else value for value in value_set
            ]
        if None in value_set:
            # spark isin returns None when any value is compared to None
            logger.error(
                "expect_column_values_to_be_in_set cannot support a None in the value_set in spark"
            )
            raise ValueError(
                "expect_column_values_to_be_in_set cannot support a None in the value_set in spark"
            )
        return column.withColumn("__success", column[0].isin(value_set))

    @DocInherit
    @MetaSparkDFExecutionEngine.column_map_expectation
    def expect_column_values_to_not_be_in_set(
        self,
        column,  # pyspark.sql.DataFrame
        value_set,  # List[Any]
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        if None in value_set:
            # spark isin returns None when any value is compared to None
            logger.error(
                "expect_column_values_to_not_be_in_set cannot support a None in the value_set in spark"
            )
            raise ValueError(
                "expect_column_values_to_not_be_in_set cannot support a None in the value_set in spark"
            )
        return column.withColumn("__success", ~column[0].isin(value_set))

    @DocInherit
    @MetaSparkDFExecutionEngine.column_map_expectation
    def expect_column_values_to_be_between(
        self,
        column,
        min_value=None,
        max_value=None,
        strict_min=False,
        strict_max=False,
        parse_strings_as_datetimes=None,
        output_strftime_format=None,
        allow_cross_type_comparisons=None,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        # NOTE: This function is implemented using native functions instead of UDFs, which is a faster
        # implementation. Please ensure new spark implementations migrate to the new style where possible
        if allow_cross_type_comparisons:
            raise ValueError(
                "Cross-type comparisons are not valid for SparkDFExecutionEngine"
            )

        if parse_strings_as_datetimes:
            min_value = parse(min_value)
            max_value = parse(max_value)

        if min_value is None and max_value is None:
            raise ValueError("min_value and max_value cannot both be None")
        elif min_value is None:
            if strict_max:
                return column.withColumn(
                    "__success",
                    when(column[0] < max_value, lit(True)).otherwise(lit(False)),
                )
            else:
                return column.withColumn(
                    "__success",
                    when(column[0] <= max_value, lit(True)).otherwise(lit(False)),
                )
        elif max_value is None:
            if strict_min:
                return column.withColumn(
                    "__success",
                    when(column[0] > min_value, lit(True)).otherwise(lit(False)),
                )
            else:
                return column.withColumn(
                    "__success",
                    when(column[0] >= min_value, lit(True)).otherwise(lit(False)),
                )
        else:
            if min_value > max_value:
                raise ValueError("minvalue cannot be greater than max_value")
            if strict_min and strict_max:
                return column.withColumn(
                    "__success",
                    when(
                        (min_value < column[0]) & (column[0] < max_value), lit(True)
                    ).otherwise(lit(False)),
                )
            elif strict_min:
                return column.withColumn(
                    "__success",
                    when(
                        (min_value < column[0]) & (column[0] <= max_value), lit(True)
                    ).otherwise(lit(False)),
                )
            elif strict_max:
                return column.withColumn(
                    "__success",
                    when(
                        (min_value <= column[0]) & (column[0] < max_value), lit(True)
                    ).otherwise(lit(False)),
                )
            else:
                return column.withColumn(
                    "__success",
                    when(
                        (min_value <= column[0]) & (column[0] <= max_value), lit(True)
                    ).otherwise(lit(False)),
                )

    @DocInherit
    @MetaSparkDFExecutionEngine.column_map_expectation
    def expect_column_value_lengths_to_be_between(
        self,
        column,
        min_value=None,
        max_value=None,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        if min_value is None and max_value is None:
            return column.withColumn("__success", lit(True))
        elif min_value is None:
            return column.withColumn(
                "__success",
                when(length_(column[0]) <= max_value, lit(True)).otherwise(lit(False)),
            )
        elif max_value is None:
            return column.withColumn(
                "__success",
                when(length_(column[0]) >= min_value, lit(True)).otherwise(lit(False)),
            )
        # FIXME: whether the below condition is enforced seems to be somewhat inconsistent

        # else:
        #     if min_value > max_value:
        #         raise ValueError("minvalue cannot be greater than max_value")

        return column.withColumn(
            "__success",
            when(
                (min_value <= length_(column[0])) & (length_(column[0]) <= max_value),
                lit(True),
            ).otherwise(lit(False)),
        )

    @DocInherit
    @MetaSparkDFExecutionEngine.column_map_expectation
    def expect_column_values_to_be_unique(
        self,
        column,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        return column.withColumn(
            "__success", count(lit(1)).over(Window.partitionBy(column[0])) <= 1
        )

    @DocInherit
    @MetaSparkDFExecutionEngine.column_map_expectation
    def expect_column_value_lengths_to_equal(
        self,
        column,
        value,  # int
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        return column.withColumn(
            "__success",
            when(length_(column[0]) == value, lit(True)).otherwise(lit(False)),
        )

    @DocInherit
    @MetaSparkDFExecutionEngine.column_map_expectation
    def expect_column_values_to_match_strftime_format(
        self,
        column,
        strftime_format,  # str
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        # Below is a simple validation that the provided format can both format and parse a datetime object.
        # %D is an example of a format that can format but not parse, e.g.
        try:
            datetime.datetime.strptime(
                datetime.datetime.strftime(datetime.datetime.now(), strftime_format),
                strftime_format,
            )
        except ValueError as e:
            raise ValueError("Unable to use provided strftime_format. " + e.message)

        def is_parseable_by_format(val):
            try:
                datetime.datetime.strptime(val, strftime_format)
                return True
            except TypeError:
                raise TypeError(
                    "Values passed to expect_column_values_to_match_strftime_format must be of type string.\nIf you want to validate a column of dates or timestamps, please call the expectation before converting from string format."
                )
            except ValueError:
                return False

        success_udf = udf(is_parseable_by_format)
        return column.withColumn("__success", success_udf(column[0]))

    @DocInherit
    @MetaSparkDFExecutionEngine.column_map_expectation
    def expect_column_values_to_not_be_null(
        self,
        column,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        return column.withColumn("__success", column[0].isNotNull())

    @DocInherit
    @MetaSparkDFExecutionEngine.column_map_expectation
    def expect_column_values_to_be_null(
        self,
        column,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        return column.withColumn("__success", column[0].isNull())

    @DocInherit
    @MetaSparkDFExecutionEngine.column_map_expectation
    def expect_column_values_to_match_json_schema(
        self,
        column,
        json_schema,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        def matches_json_schema(val):
            try:
                val_json = json.loads(val)
                jsonschema.validate(val_json, json_schema)
                # jsonschema.validate raises an error if validation fails.
                # So if we make it this far, we know that the validation succeeded.
                return True
            except jsonschema.ValidationError:
                return False
            except jsonschema.SchemaError:
                raise
            except:
                raise

        matches_json_schema_udf = udf(matches_json_schema, sparktypes.StringType())

        return column.withColumn("__success", matches_json_schema_udf(column[0]))

    @DocInherit
    @DataAsset.expectation(["column", "type_", "mostly"])
    def expect_column_values_to_be_of_type(
        self,
        column,
        type_,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        # Rename column so we only have to handle dot notation here
        eval_col = "__eval_col_" + column.replace(".", "__").replace("`", "_")
        spark_df = self.dataframe.withColumn(eval_col, col(column))
        if mostly is not None:
            raise ValueError(
                "SparkDFExecutionEngine does not support column map semantics for column types"
            )

        try:
            col_df = spark_df.select(eval_col)
            col_data = [f for f in col_df.schema.fields if f.name == eval_col][0]
            col_type = type(col_data.dataType)
        except IndexError:
            raise ValueError("Unrecognized column: %s" % column)
        except KeyError:
            raise ValueError("No type data available for column: %s" % column)

        try:
            if type_ is None:
                # vacuously true
                success = True
            else:
                success = issubclass(col_type, getattr(sparktypes, type_))

            return {"success": success, "result": {"observed_value": col_type.__name__}}

        except AttributeError:
            raise ValueError("Unrecognized spark type: %s" % type_)

    @DocInherit
    @DataAsset.expectation(["column", "type_list", "mostly"])
    def expect_column_values_to_be_in_type_list(
        self,
        column,
        type_list: List[str],
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        # Rename column so we only have to handle dot notation here
        eval_col = "__eval_col_" + column.replace(".", "__").replace("`", "_")
        spark_df = self.dataframe.withColumn(eval_col, col(column))

        if mostly is not None:
            raise ValueError(
                "SparkDFExecutionEngine does not support column map semantics for column types"
            )

        try:
            col_df = spark_df.select(eval_col)
            col_data = [f for f in col_df.schema.fields if f.name == eval_col][0]
            col_type = type(col_data.dataType)
        except IndexError:
            raise ValueError("Unrecognized column: %s" % column)
        except KeyError:
            raise ValueError("No database type data available for column: %s" % column)

        if type_list is None:
            success = True
        else:
            types = []
            for type_ in type_list:
                try:
                    type_class = getattr(sparktypes, type_)
                    types.append(type_class)
                except AttributeError:
                    logger.debug("Unrecognized type: %s" % type_)
            if len(types) == 0:
                raise ValueError("No recognized spark types in type_list")
            types = tuple(types)
            success = issubclass(col_type, types)
        return {"success": success, "result": {"observed_value": col_type.__name__}}

    @DocInherit
    @MetaSparkDFExecutionEngine.column_map_expectation
    def expect_column_values_to_match_regex(
        self,
        column,
        regex,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        return column.withColumn("__success", column[0].rlike(regex))

    @DocInherit
    @MetaSparkDFExecutionEngine.column_map_expectation
    def expect_column_values_to_not_match_regex(
        self,
        column,
        regex,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        return column.withColumn("__success", ~column[0].rlike(regex))

    @DocInherit
    @MetaSparkDFExecutionEngine.column_map_expectation
    def expect_column_values_to_match_regex_list(
        self,
        column,
        regex_list,
        match_on="any",
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        if match_on == "any":
            return column.withColumn("__success", column[0].rlike("|".join(regex_list)))
        elif match_on == "all":
            formatted_regex_list = ["(?={})".format(regex) for regex in regex_list]
            return column.withColumn(
                "__success", column[0].rlike("".join(formatted_regex_list))
            )
        else:
            raise ValueError("match_on must be either 'any' or 'all'")

    @DocInherit
    @MetaSparkDFExecutionEngine.column_pair_map_expectation
    def expect_column_pair_values_to_be_equal(
        self,
        column_A,
        column_B,
        ignore_row_if="both_values_are_missing",
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        column_A_name = column_A.schema.names[1]
        column_B_name = column_B.schema.names[1]
        join_df = column_A.join(
            column_B, column_A["__row"] == column_B["__row"], how="inner"
        )
        return join_df.withColumn(
            "__success",
            when(col(column_A_name) == col(column_B_name), True).otherwise(False),
        )

    @DocInherit
    @MetaSparkDFExecutionEngine.column_pair_map_expectation
    def expect_column_pair_values_A_to_be_greater_than_B(
        self,
        column_A,
        column_B,
        or_equal=None,
        parse_strings_as_datetimes=None,
        allow_cross_type_comparisons=None,
        ignore_row_if="both_values_are_missing",
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        # FIXME
        if allow_cross_type_comparisons:
            raise NotImplementedError

        column_A_name = column_A.schema.names[1]
        column_B_name = column_B.schema.names[1]

        if parse_strings_as_datetimes:
            _udf = udf(parse, sparktypes.TimestampType())
            # Create new columns for comparison without replacing original values.
            (timestamp_column_A, timestamp_column_B) = (
                "__ts_{}".format(column_A_name),
                "__ts_{}".format(column_B_name),
            )
            temp_column_A = column_A.withColumn(timestamp_column_A, _udf(column_A_name))
            temp_column_B = column_B.withColumn(timestamp_column_B, _udf(column_B_name))
            # Use the new columns to compare instead of original columns.
            (column_A_name, column_B_name) = (timestamp_column_A, timestamp_column_B)

        else:
            temp_column_A = column_A
            temp_column_B = column_B

        join_df = temp_column_A.join(
            temp_column_B, temp_column_A["__row"] == temp_column_B["__row"], how="inner"
        )

        if or_equal:
            return join_df.withColumn(
                "__success",
                when(col(column_A_name) >= col(column_B_name), True).otherwise(False),
            )
        else:
            return join_df.withColumn(
                "__success",
                when(col(column_A_name) > col(column_B_name), True).otherwise(False),
            )

    @DocInherit
    @MetaSparkDFExecutionEngine.column_pair_map_expectation
    def expect_column_pair_values_to_be_in_set(
        self,
        column_A,
        column_B,
        value_pairs_set,  # List[List]
        ignore_row_if="both_values_are_missing",
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        column_A_name = column_A.schema.names[1]
        column_B_name = column_B.schema.names[1]

        join_df = column_A.join(
            column_B, column_A["__row"] == column_B["__row"], how="inner"
        )

        join_df = join_df.withColumn(
            "combine_AB", array(col(column_A_name), col(column_B_name))
        )

        value_set_df = (
            SQLContext(self.dataframe._sc)
            .createDataFrame(value_pairs_set, ["col_A", "col_B"])
            .select(array("col_A", "col_B").alias("set_AB"))
        )

        return join_df.join(
            value_set_df, join_df["combine_AB"] == value_set_df["set_AB"], "left"
        ).withColumn(
            "__success", when(col("set_AB").isNull(), lit(False)).otherwise(lit(True))
        )

    @DocInherit
    @MetaSparkDFExecutionEngine.multicolumn_map_expectation
    def expect_multicolumn_values_to_be_unique(
        self,
        column_list,  # pyspark.sql.DataFrame
        ignore_row_if="all_values_are_missing",
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        # Might want to throw an exception if only 1 column is passed
        column_names = column_list.schema.names[:]
        conditions = []
        for i in range(0, len(column_names) - 1):
            # Negate the `eqNullSafe` result and append to the conditions.
            conditions.append(
                ~(col(column_names[i]).eqNullSafe(col(column_names[i + 1])))
            )

        return column_list.withColumn(
            "__success", reduce(lambda a, b: a & b, conditions)
        )

    @DocInherit
    @MetaSparkDFExecutionEngine.column_map_expectation
    def expect_column_values_to_be_increasing(
        self,
        column,  # pyspark.sql.DataFrame
        strictly=False,
        mostly=None,
        parse_strings_as_datetimes=None,
        output_strftime_format=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        # string column name
        column_name = column.schema.names[0]
        # check if column is any type that could have na (numeric types)
        na_types = [
            isinstance(column.schema[column_name].dataType, typ)
            for typ in [
                sparktypes.LongType,
                sparktypes.DoubleType,
                sparktypes.IntegerType,
            ]
        ]

        # if column is any type that could have NA values, remove them (not filtered by .isNotNull())
        if any(na_types):
            column = column.filter(~isnan(column[0]))

        if parse_strings_as_datetimes:
            # convert column to timestamp format
            column = self._apply_dateutil_parse(column)
            # create constant column to order by in window function to preserve order of original df
            column = column.withColumn("constant", lit("constant")).withColumn(
                "lag", lag(column[0]).over(Window.orderBy(col("constant")))
            )

            column = column.withColumn("diff", datediff(col(column_name), col("lag")))

        else:
            column = (
                column.withColumn("constant", lit("constant"))
                .withColumn("lag", lag(column[0]).over(Window.orderBy(col("constant"))))
                .withColumn("diff", column[0] - col("lag"))
            )

        # replace lag first row null with 1 so that it is not flagged as fail
        column = column.withColumn(
            "diff", when(col("diff").isNull(), 1).otherwise(col("diff"))
        )

        if strictly:
            return column.withColumn(
                "__success", when(col("diff") >= 1, lit(True)).otherwise(lit(False))
            )

        else:
            return column.withColumn(
                "__success", when(col("diff") >= 0, lit(True)).otherwise(lit(False))
            )

    @DocInherit
    @MetaSparkDFExecutionEngine.column_map_expectation
    def expect_column_values_to_be_decreasing(
        self,
        column,  # pyspark.sql.DataFrame
        strictly=False,
        mostly=None,
        parse_strings_as_datetimes=None,
        output_strftime_format=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        # string column name
        column_name = column.schema.names[0]
        # check if column is any type that could have na (numeric types)
        na_types = [
            isinstance(column.schema[column_name].dataType, typ)
            for typ in [
                sparktypes.LongType,
                sparktypes.DoubleType,
                sparktypes.IntegerType,
            ]
        ]

        # if column is any type that could have NA values, remove them (not filtered by .isNotNull())
        if any(na_types):
            column = column.filter(~isnan(column[0]))

        if parse_strings_as_datetimes:
            # convert column to timestamp format
            column = self._apply_dateutil_parse(column)
            # create constant column to order by in window function to preserve order of original df
            column = column.withColumn("constant", lit("constant")).withColumn(
                "lag", lag(column[0]).over(Window.orderBy(col("constant")))
            )

            column = column.withColumn("diff", datediff(col(column_name), col("lag")))

        else:
            column = (
                column.withColumn("constant", lit("constant"))
                .withColumn("lag", lag(column[0]).over(Window.orderBy(col("constant"))))
                .withColumn("diff", column[0] - col("lag"))
            )

        # replace lag first row null with -1 so that it is not flagged as fail
        column = column.withColumn(
            "diff", when(col("diff").isNull(), -1).otherwise(col("diff"))
        )

        if strictly:
            return column.withColumn(
                "__success", when(col("diff") <= -1, lit(True)).otherwise(lit(False))
            )

        else:
            return column.withColumn(
                "__success", when(col("diff") <= 0, lit(True)).otherwise(lit(False))
            )