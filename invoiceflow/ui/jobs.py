"""Run the pipeline on a worker thread and expose its REAL stage events to the page.

Why a thread: Streamlit re-runs (and interrupts) the page script on every interaction. If the pipeline ran
inside the script, a click or navigation mid-run could abort it half-way. On a worker thread the run always
finishes and is fully recorded; the page simply re-attaches to the same job on its next run, so repeated
clicks can never start a second run. The page polls ``snapshot()``; there are no artificial delays. It
only waits for events the pipeline actually emits.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from invoiceflow import constants
from invoiceflow.schemas import InvoiceExtraction, PipelineResult, StageEvent
from invoiceflow.services.pipeline import InvoicePipeline

logger = logging.getLogger(__name__)


class PipelineJob:
    def __init__(
        self,
        pipeline: InvoicePipeline,
        data: bytes,
        filename: str,
        content_type: str | None,
        *,
        key: Any,
        manual: InvoiceExtraction | None = None,
    ) -> None:
        self.key = key  # identifies the upload this job belongs to
        self.manual = manual is not None
        self.result: PipelineResult | None = None
        self.exception: BaseException | None = None
        self.done = threading.Event()
        self._lock = threading.Lock()
        self._events: dict[str, StageEvent] = {n: StageEvent(stage_name=n, title=t) for n, t in constants.PIPELINE_STAGES}
        self._thread = threading.Thread(
            target=self._run, args=(pipeline, data, filename, content_type, manual), name="invoice-pipeline", daemon=True
        )
        self._thread.start()

    def _on_event(self, event: StageEvent) -> None:
        with self._lock:
            self._events[event.stage_name] = event

    def _run(
        self, pipeline: InvoicePipeline, data: bytes, filename: str, content_type: str | None, manual: InvoiceExtraction | None
    ) -> None:
        try:
            self.result = pipeline.process(data, filename, content_type=content_type, manual_extraction=manual, on_event=self._on_event)
        except BaseException as exc:
            logger.exception("Pipeline job crashed")
            self.exception = exc
        finally:
            self.done.set()

    def snapshot(self) -> list[StageEvent]:
        """Current state of every stage, in execution order."""
        with self._lock:
            return [self._events[n].model_copy() for n, _ in constants.PIPELINE_STAGES]

    @property
    def running(self) -> bool:
        return not self.done.is_set()
