from ray.tests.conftest import *  # noqa

import pytest

import ray
from ray import workflow
from ray.experimental.dag import InputNode


def test_dag_to_workflow_execution(workflow_start_regular_shared):
    """This test constructs a DAG with complex dependencies
    and turns it into a workflow."""

    @ray.remote
    def begin(x, pos, a):
        return x * a + pos  # 23.14

    @ray.remote
    def left(x, c, a):
        return f"left({x}, {c}, {a})"

    @ray.remote
    def right(x, b, pos):
        return f"right({x}, {b}, {pos})"

    @ray.remote
    def end(lf, rt, b):
        return f"{lf},{rt};{b}"

    with InputNode() as dag_input:
        f = begin._bind(2, dag_input[1], a=dag_input.a)
        lf = left._bind(f, "hello", dag_input.a)
        rt = right._bind(f, b=dag_input.b, pos=dag_input[0])
        b = end._bind(lf, rt, b=dag_input.b)

    wf = workflow.create(b, 2, 3.14, a=10, b="ok")
    assert len(list(wf._iter_workflows_in_dag())) == 4, "incorrect amount of steps"
    assert wf.run() == "left(23.14, hello, 10),right(23.14, ok, 2);ok"


def test_dag_to_workflow_options(workflow_start_regular_shared):
    """This test if the workflow inherits DAG options."""

    @ray.remote
    def no_resource():
        pass

    # TODO(suquark): The current Ray DAG is buggy, it failed to return the
    # "original" options, we need to override "num_returns" to pass workflow check.
    dag = no_resource.options(num_gpus=100, num_returns=1)._bind()

    wf = workflow.create(dag)
    assert wf.data.step_options.ray_options["num_gpus"] == 100


def test_dedupe_serialization_dag(workflow_start_regular_shared):
    from ray.workflow import serialization

    @ray.remote
    def identity(x):
        return x

    @ray.remote
    def gather(*args):
        return args

    def get_num_uploads():
        manager = serialization.get_or_create_manager()
        stats = ray.get(manager.export_stats.remote())
        return stats.get("num_uploads", 0)

    ref = ray.put("hello world 12345")
    list_of_refs = [ref for _ in range(20)]

    assert get_num_uploads() == 0

    single = identity._bind((ref,))
    double = identity._bind(list_of_refs)

    result_ref, result_list = workflow.create(gather._bind(single, double)).run()

    for result in result_list:
        assert ray.get(*result_ref) == ray.get(result)

    # One upload for the initial checkpoint, and one for the object ref after
    # resuming.
    assert get_num_uploads() == 2


def test_same_object_many_dags(workflow_start_regular_shared):
    """Ensure that when we dedupe uploads, we upload the object once per DAG,
    since different DAGs shouldn't look in each others object directories.
    """

    @ray.remote
    def f(a):
        return [a[0]]

    x = {0: ray.put(10)}

    result1 = workflow.create(f._bind(x)).run()
    result2 = workflow.create(f._bind(x)).run()
    with InputNode() as dag_input:
        result3 = workflow.create(f._bind(dag_input.x), x=x).run()

    assert ray.get(*result1) == 10
    assert ray.get(*result2) == 10
    assert ray.get(*result3) == 10


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main(["-v", __file__]))
