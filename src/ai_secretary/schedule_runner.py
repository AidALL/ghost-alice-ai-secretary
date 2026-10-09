"""Fixed scheduled occurrence entry point. Never creates an input admission."""

import argparse
import asyncio
import inspect
import json
import signal

from .coordinator import AuthorityDenied


async def run_occurrence(occurrence_id, *, store, jobs, coordinator, collection_factory):
    if jobs.store is not store or jobs.coordinator is not coordinator:
        raise ValueError("scheduled services must share the authority store and coordinator")
    claimed = await jobs.claim(occurrence_id)
    if claimed.context is None:
        return {"status": claimed.state, "disposition": claimed.disposition, "occurrence_id": occurrence_id}
    context = claimed.context
    try:
        await coordinator.checkpoint(context, "scheduled-collect")
        collector = collection_factory()
        if inspect.isawaitable(collector):
            collector = await collector
        result = await collector.collect(context, await coordinator.source(context))
        if result.status not in ("Completed", "Pending", "Unknown", "Cancelled", "Failed"):
            raise ValueError("collection returned an unsupported run outcome")
        if result.status == "Completed":
            await coordinator.checkpoint(context, "scheduled-finish")
        await jobs.finish(context, result.status, live_result=True)
        return result.as_dict()
    except AuthorityDenied as error:
        # Unknown accounting preserves unresolved retrieval hazards after revoke.
        await jobs.finish(context, "Unknown")
        return {"status": "Unknown", "disposition": error.disposition, "occurrence_id": occurrence_id}
    except BaseException:
        # Cancellation propagates only after the awaited collector unwinds. The
        # occurrence cannot recur until its uncertain effects are reconciled.
        await asyncio.shield(jobs.finish(context, "Unknown"))
        raise


async def _main(state_dir, occurrence_id):
    from .service import build_scheduled_services
    store, jobs, coordinator, collection_factory = build_scheduled_services(state_dir)
    task = asyncio.create_task(run_occurrence(occurrence_id, store=store, jobs=jobs,
                                             coordinator=coordinator,
                                             collection_factory=collection_factory))
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, task.cancel)
    try:
        result = await task
        print(json.dumps(result, separators=(",", ":")))
        return 0 if result["status"] == "Completed" else 2
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Execute one durable Secretary occurrence")
    parser.add_argument("--state", required=True)
    parser.add_argument("--occurrence", required=True)
    options = parser.parse_args(argv)
    try:
        return asyncio.run(_main(options.state, options.occurrence))
    except asyncio.CancelledError:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
