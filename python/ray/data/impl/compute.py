from typing import TypeVar, Any, Union, Callable, List, Tuple, Optional

import ray
from ray.util.annotations import PublicAPI, DeveloperAPI
from ray.data.block import (
    Block,
    BlockAccessor,
    BlockMetadata,
    BlockPartition,
    BlockExecStats,
)
from ray.data.context import DatasetContext
from ray.data.impl.delegating_block_builder import DelegatingBlockBuilder
from ray.data.impl.block_list import BlockList
from ray.data.impl.progress_bar import ProgressBar
from ray.data.impl.remote_fn import cached_remote_fn

T = TypeVar("T")
U = TypeVar("U")

# A class type that implements __call__.
CallableClass = type


@DeveloperAPI
class ComputeStrategy:
    def _apply(self, fn: Any, blocks: BlockList, clear_input_blocks: bool) -> BlockList:
        raise NotImplementedError


@DeveloperAPI
class TaskPoolStrategy(ComputeStrategy):
    def _apply(
        self,
        fn: Any,
        remote_args: dict,
        block_list: BlockList,
        clear_input_blocks: bool,
    ) -> BlockList:
        context = DatasetContext.get_current()

        # Handle empty datasets.
        if block_list.initial_num_blocks() == 0:
            return block_list

        blocks = block_list.get_blocks_with_metadata()
        map_bar = ProgressBar("Map Progress", total=len(blocks))

        if context.block_splitting_enabled:
            map_block = cached_remote_fn(_map_block_split).options(**remote_args)
            refs = [map_block.remote(b, fn, m.input_files) for b, m in blocks]
        else:
            map_block = cached_remote_fn(_map_block_nosplit).options(
                **dict(remote_args, num_returns=2)
            )
            all_refs = [map_block.remote(b, fn, m.input_files) for b, m in blocks]
            data_refs = [r[0] for r in all_refs]
            refs = [r[1] for r in all_refs]

        # Release input block references.
        if clear_input_blocks:
            del blocks
            block_list.clear()

        # Common wait for non-data refs.
        try:
            results = map_bar.fetch_until_complete(refs)
        except (ray.exceptions.RayTaskError, KeyboardInterrupt) as e:
            # One or more mapper tasks failed, or we received a SIGINT signal
            # while waiting; either way, we cancel all map tasks.
            for ref in refs:
                ray.cancel(ref)
            # Wait until all tasks have failed or been cancelled.
            for ref in refs:
                try:
                    ray.get(ref)
                except (ray.exceptions.RayTaskError, ray.exceptions.TaskCancelledError):
                    pass
            # Reraise the original task failure exception.
            raise e from None

        new_blocks, new_metadata = [], []
        if context.block_splitting_enabled:
            for result in results:
                for block, metadata in result:
                    new_blocks.append(block)
                    new_metadata.append(metadata)
        else:
            for block, metadata in zip(data_refs, results):
                new_blocks.append(block)
                new_metadata.append(metadata)
        return BlockList(list(new_blocks), list(new_metadata))


@PublicAPI
class ActorPoolStrategy(ComputeStrategy):
    """Specify the compute strategy for a Dataset transform.

    ActorPool specifies that an autoscaling pool of actors should be used for a given
    Dataset transform. This is useful for stateful setup of callable classes.

    To autoscale from ``m`` to ``n`` actors, specify ``compute=ActorPool(m, n)``.
    For a fixed-sized pool of size ``n``, specify ``compute=ActorPool(n, n)``.
    """

    def __init__(self, min_size: int = 1, max_size: Optional[int] = None):
        if min_size < 1:
            raise ValueError("min_size must be > 1", min_size)
        if max_size is not None and min_size > max_size:
            raise ValueError("min_size must be <= max_size", min_size, max_size)
        self.min_size = min_size
        self.max_size = max_size or float("inf")

    def _apply(
        self,
        fn: Any,
        remote_args: dict,
        block_list: BlockList,
        clear_input_blocks: bool,
    ) -> BlockList:
        """Note: this is not part of the Dataset public API."""
        context = DatasetContext.get_current()

        blocks_in = block_list.get_blocks_with_metadata()

        # Early release block references.
        if clear_input_blocks:
            block_list.clear()

        orig_num_blocks = len(blocks_in)
        results = []
        map_bar = ProgressBar("Map Progress", total=orig_num_blocks)

        class BlockWorker:
            def ready(self):
                return "ok"

            def map_block_split(
                self, block: Block, input_files: List[str]
            ) -> BlockPartition:
                return _map_block_split(block, fn, input_files)

            @ray.method(num_returns=2)
            def map_block_nosplit(
                self, block: Block, input_files: List[str]
            ) -> Tuple[Block, BlockMetadata]:
                return _map_block_nosplit(block, fn, input_files)

        if not remote_args:
            remote_args["num_cpus"] = 1

        BlockWorker = ray.remote(**remote_args)(BlockWorker)

        workers = [BlockWorker.remote() for _ in range(self.min_size)]
        tasks = {w.ready.remote(): w for w in workers}
        metadata_mapping = {}
        ready_workers = set()

        while len(results) < orig_num_blocks:
            ready, _ = ray.wait(
                list(tasks), timeout=0.01, num_returns=1, fetch_local=False
            )
            if not ready:
                if (
                    len(workers) < self.max_size
                    and len(ready_workers) / len(workers) > 0.8
                ):
                    w = BlockWorker.remote()
                    workers.append(w)
                    tasks[w.ready.remote()] = w
                    map_bar.set_description(
                        "Map Progress ({} actors {} pending)".format(
                            len(ready_workers), len(workers) - len(ready_workers)
                        )
                    )
                continue

            [obj_id] = ready
            worker = tasks[obj_id]
            del tasks[obj_id]

            # Process task result.
            if worker in ready_workers:
                results.append(obj_id)
                map_bar.update(1)
            else:
                ready_workers.add(worker)
                map_bar.set_description(
                    "Map Progress ({} actors {} pending)".format(
                        len(ready_workers), len(workers) - len(ready_workers)
                    )
                )

            # Schedule a new task.
            if blocks_in:
                block, meta = blocks_in.pop()
                if context.block_splitting_enabled:
                    ref = worker.map_block_split.remote(block, meta.input_files)
                else:
                    ref, meta_ref = worker.map_block_nosplit.remote(
                        block, meta.input_files
                    )
                    metadata_mapping[ref] = meta_ref
                tasks[ref] = worker

        map_bar.close()
        new_blocks, new_metadata = [], []
        if context.block_splitting_enabled:
            for result in ray.get(results):
                for block, metadata in result:
                    new_blocks.append(block)
                    new_metadata.append(metadata)
        else:
            for block in results:
                new_blocks.append(block)
                new_metadata.append(metadata_mapping[block])
            new_metadata = ray.get(new_metadata)
        return BlockList(new_blocks, new_metadata)


def cache_wrapper(
    fn: Union[CallableClass, Callable[[Any], Any]],
    compute: Optional[Union[str, ComputeStrategy]],
) -> Callable[[Any], Any]:
    """Implements caching of stateful callables.

    Args:
        fn: Either a plain function or class of a stateful callable.

    Returns:
        A plain function with per-process initialization cached as needed.
    """
    if isinstance(fn, CallableClass):

        if compute is None:
            raise ValueError(
                "``compute`` must be specified when using a callable class. "
                'For example, use ``compute="actors"`` or '
                "``compute=ActorPoolStrategy(min, max)``."
            )

        def _fn(item: Any) -> Any:
            if ray.data._cached_fn is None or ray.data._cached_cls != fn:
                ray.data._cached_cls = fn
                ray.data._cached_fn = fn()
            return ray.data._cached_fn(item)

        return _fn
    else:
        return fn


def get_compute(compute_spec: Union[str, ComputeStrategy]) -> ComputeStrategy:
    if not compute_spec or compute_spec == "tasks":
        return TaskPoolStrategy()
    elif compute_spec == "actors":
        return ActorPoolStrategy()
    elif isinstance(compute_spec, ComputeStrategy):
        return compute_spec
    else:
        raise ValueError("compute must be one of [`tasks`, `actors`, ComputeStrategy]")


def _map_block_split(block: Block, fn: Any, input_files: List[str]) -> BlockPartition:
    output = []
    stats = BlockExecStats.builder()
    for new_block in fn(block):
        accessor = BlockAccessor.for_block(new_block)
        new_meta = BlockMetadata(
            num_rows=accessor.num_rows(),
            size_bytes=accessor.size_bytes(),
            schema=accessor.schema(),
            input_files=input_files,
            exec_stats=stats.build(),
        )
        owner = DatasetContext.get_current().block_owner
        output.append((ray.put(new_block, _owner=owner), new_meta))
        stats = BlockExecStats.builder()
    return output


def _map_block_nosplit(
    block: Block, fn: Any, input_files: List[str]
) -> Tuple[Block, BlockMetadata]:
    stats = BlockExecStats.builder()
    builder = DelegatingBlockBuilder()
    for new_block in fn(block):
        builder.add_block(new_block)
    new_block = builder.build()
    accessor = BlockAccessor.for_block(new_block)
    return new_block, accessor.get_metadata(
        input_files=input_files, exec_stats=stats.build()
    )
