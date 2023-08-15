# Databricks notebook source
##################################################################################
# Model Validation Notebook
##
# This notebook uses mlflow model validation API to run mode validation after training and registering a model
# in model registry, before deploying it to Production stage.
#
# It runs as part of CD and by an automated model training job -> validation -> deployment job defined under ``gh_mlops_stack_dab/databricks-resources/model-workflow-resource.yml``
#
#
# Parameters:
#
# * env                                     - Name of the environment the notebook is run in (staging, or prod). Defaults to "prod".
# * `run_mode`                              - The `run_mode` defines whether model validation is enabled or not. It can be one of the three values:
#                                             * `disabled` : Do not run the model validation notebook.
#                                             * `dry_run`  : Run the model validation notebook. Ignore failed model validation rules and proceed to move
#                                                            model to Production stage.
#                                             * `enabled`  : Run the model validation notebook. Move model to Production stage only if all model validation
#                                                            rules are passing.
# * enable_baseline_comparison              - Whether to load the current registered "Production" stage model as baseline.
#                                             Baseline model is a requirement for relative change and absolute change validation thresholds.
# * validation_input                        - Validation input. Please refer to data parameter in mlflow.evaluate documentation https://mlflow.org/docs/latest/python_api/mlflow.html#mlflow.evaluate

# * custom_metrics_loader_function          - Specifies the name of the function in gh-mlops-stack-dab/validation/validation.py that returns custom metrics.
# * validation_thresholds_loader_function   - Specifies the name of the function in gh-mlops-stack-dab/validation/validation.py that returns model validation thresholds.
#
# For details on mlflow evaluate API, see doc https://mlflow.org/docs/latest/python_api/mlflow.html#mlflow.evaluate
# For details and examples about performing model validation, see the Model Validation documentation https://mlflow.org/docs/latest/models.html#model-validation
#
##################################################################################

# COMMAND ----------

# MAGIC %load_ext autoreload
# MAGIC %autoreload 2

# COMMAND ----------

import os
notebook_path =  '/Workspace/' + os.path.dirname(dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get())
%cd $notebook_path

# COMMAND ----------

# MAGIC %pip install -r ../../requirements.txt

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

import os
notebook_path =  '/Workspace/' + os.path.dirname(dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get())
%cd $notebook_path
%cd ../

# COMMAND ----------

dbutils.widgets.dropdown(
    "env", "prod", ["staging", "prod"], "Environment(for input data)"
)
dbutils.widgets.text(
    "experiment_name",
    "/dev-gh-mlops-stack-dab-experiment",
    "Experiment Name",
)
dbutils.widgets.dropdown("run_mode", "disabled", ["disabled", "dry_run", "enabled"], "Run Mode")
dbutils.widgets.dropdown("enable_baseline_comparison", "false", ["true", "false"], "Enable Baseline Comparison")
dbutils.widgets.text("validation_input", "SELECT * FROM delta.`dbfs:/databricks-datasets/nyctaxi-with-zipcodes/subsampled`", "Validation Input")

dbutils.widgets.text("custom_metrics_loader_function", "custom_metrics", "Custom Metrics Loader Function")
dbutils.widgets.text("validation_thresholds_loader_function", "validation_thresholds", "Validation Thresholds Loader Function")
dbutils.widgets.text("evaluator_config_loader_function", "evaluator_config", "Evaluator Config Loader Function")
dbutils.widgets.text("model_name", "dev-gh-mlops-stack-dab-model", "Model Name")
dbutils.widgets.text("model_version", "", "Candidate Model Version")

# COMMAND ----------



run_mode = dbutils.widgets.get("run_mode").lower()
assert run_mode == "disabled" or run_mode == "dry_run" or run_mode == "enabled"

if run_mode == "disabled":
    print(
        "Model validation is in DISABLED mode. Exit model validation without blocking model deployment."
    )
    dbutils.notebook.exit(0)
dry_run = run_mode == "dry_run"

if dry_run:
    print(
        "Model validation is in DRY_RUN mode. Validation threshold validation failures will not block model deployment."
    )
else:
    print(
        "Model validation is in ENABLED mode. Validation threshold validation failures will block model deployment."
    )

# COMMAND ----------

import importlib
import mlflow
import os
import tempfile
import traceback
from mlflow.recipes.utils import (
    get_recipe_config,
    get_recipe_name,
    get_recipe_root_path,
)
from mlflow.tracking.client import MlflowClient

client = MlflowClient()

experiment_name = dbutils.widgets.get("experiment_name")

env = dbutils.widgets.get("env")
assert env, "env notebook parameter must be specified"

def get_model_type_from_recipe():
    try:
        recipe_config = get_recipe_config("../training", f"databricks-{env}")
        problem_type = recipe_config.get("recipe").split("/")[0]
        if problem_type.lower() == "regression":
            return "regressor"
        elif problem_type.lower() == "classification":
            return "classifier"
        else:
            raise Exception(f"Unsupported recipe {recipe_config}")
    except Exception as ex:
        print(f"Not able to get model type from mlflow recipe databricks-{env}.")
        raise ex

def get_targets_from_recipe():
    try:
        recipe_config = get_recipe_config("../training", f"databricks-{env}")
        return recipe_config.get("target_col")
    except Exception as ex:
        print(f"Not able to get targets from mlflow recipe databricks-{env}.")
        raise ex


# set model evaluation parameters that can be inferred from the job
model_uri = dbutils.jobs.taskValues.get("Train", "model_uri", debugValue="")
model_name = dbutils.jobs.taskValues.get("Train", "model_name", debugValue="")
model_version = dbutils.jobs.taskValues.get("Train", "model_version", debugValue="")

if model_uri == "":
    model_name = dbutils.widgets.get("model_name")
    model_version = dbutils.widgets.get("model_version")
    model_uri = "models:/" + model_name + "/" + model_version

baseline_model_uri = "models:/" + model_name + "/Production"
evaluators = "default"
assert model_uri != "", "model_uri notebook parameter must be specified"
assert model_name != "", "model_name notebook parameter must be specified"
assert model_version != "", "model_version notebook parameter must be specified"

# set experiment
mlflow.set_experiment(experiment_name)

# COMMAND ----------

# take input

enable_baseline_comparison = dbutils.widgets.get("enable_baseline_comparison")
assert enable_baseline_comparison == "true" or enable_baseline_comparison == "false"
enable_baseline_comparison = enable_baseline_comparison == "true"

validation_input = dbutils.widgets.get("validation_input")
assert validation_input
data = spark.sql(validation_input)

model_type = get_model_type_from_recipe()
targets = get_targets_from_recipe()
assert model_type
assert targets

custom_metrics_loader_function_name = dbutils.widgets.get("custom_metrics_loader_function")
validation_thresholds_loader_function_name = dbutils.widgets.get("validation_thresholds_loader_function")
evaluator_config_loader_function_name = dbutils.widgets.get("evaluator_config_loader_function")
assert custom_metrics_loader_function_name
assert validation_thresholds_loader_function_name
assert evaluator_config_loader_function_name
custom_metrics_loader_function = getattr(
    importlib.import_module("validation"), custom_metrics_loader_function_name
)
validation_thresholds_loader_function = getattr(
    importlib.import_module("validation"), validation_thresholds_loader_function_name
)
evaluator_config_loader_function = getattr(
    importlib.import_module("validation"), evaluator_config_loader_function_name
)
custom_metrics = custom_metrics_loader_function()
validation_thresholds = validation_thresholds_loader_function()
evaluator_config = evaluator_config_loader_function()

# COMMAND ----------

# helper methods
def get_run_link(run_info):
    return "[Run](#mlflow/experiments/{0}/runs/{1})".format(
        run_info.experiment_id, run_info.run_id
    )


def get_training_run(model_name, model_version):
    version = client.get_model_version(model_name, model_version)
    return mlflow.get_run(run_id=version.run_id)


def generate_run_name(training_run):
    return None if not training_run else training_run.info.run_name + "-validation"


def generate_description(training_run):
    return (
        None
        if not training_run
        else "Model Training Details: {0}\n".format(get_run_link(training_run.info))
    )


def log_to_model_description(run, success):
    run_link = get_run_link(run.info)
    description = client.get_model_version(model_name, model_version).description
    status = "SUCCESS" if success else "FAILURE"
    if description != "":
        description += "\n\n---\n\n"
    description += "Model Validation Status: {0}\nValidation Details: {1}".format(
        status, run_link
    )
    client.update_model_version(
        name=model_name, version=model_version, description=description
    )


# COMMAND ----------

training_run = get_training_run(model_name, model_version)
# run evaluate
with mlflow.start_run(
    run_name=generate_run_name(training_run),
    description=generate_description(training_run),
) as run, tempfile.TemporaryDirectory() as tmp_dir:
    validation_thresholds_file = os.path.join(tmp_dir, "validation_thresholds.txt")
    with open(validation_thresholds_file, "w") as f:
        if validation_thresholds:
            for metric_name in validation_thresholds:
                f.write(
                    "{0:30}  {1}\n".format(
                        metric_name, str(validation_thresholds[metric_name])
                    )
                )
    mlflow.log_artifact(validation_thresholds_file)

    try:
        eval_result = mlflow.evaluate(
            model=model_uri,
            data=data,
            targets=targets,
            model_type=model_type,
            evaluators=evaluators,
            validation_thresholds=validation_thresholds,
            custom_metrics=custom_metrics,
            baseline_model=None
            if not enable_baseline_comparison
            else baseline_model_uri,
            evaluator_config=evaluator_config,
        )
        metrics_file = os.path.join(tmp_dir, "metrics.txt")
        with open(metrics_file, "w") as f:
            f.write(
                "{0:30}  {1:30}  {2}\n".format("metric_name", "candidate", "baseline")
            )
            for metric in eval_result.metrics:
                candidate_metric_value = str(eval_result.metrics[metric])
                baseline_metric_value = "N/A"
                if metric in eval_result.baseline_model_metrics:
                    mlflow.log_metric(
                        "baseline_" + metric, eval_result.baseline_model_metrics[metric]
                    )
                    baseline_metric_value = str(
                        eval_result.baseline_model_metrics[metric]
                    )
                f.write(
                    "{0:30}  {1:30}  {2}\n".format(
                        metric, candidate_metric_value, baseline_metric_value
                    )
                )
        mlflow.log_artifact(metrics_file)
        log_to_model_description(run, True)
    except Exception as err:
        log_to_model_description(run, False)
        error_file = os.path.join(tmp_dir, "error.txt")
        with open(error_file, "w") as f:
            f.write("Validation failed : " + str(err) + "\n")
            f.write(traceback.format_exc())
        mlflow.log_artifact(error_file)
        if not dry_run:
            raise err
        else:
            print(
                "Model validation failed in DRY_RUN. It will not block model deployment."
            )

# COMMAND ----------
