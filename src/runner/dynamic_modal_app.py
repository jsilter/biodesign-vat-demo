"""Fixed no-network Modal function for approved generated CPU jobs."""

from __future__ import annotations

try:
    import modal
except ImportError:  # pragma: no cover
    modal = None

from vatlog import vat
from .dynamic_worker import dynamic_result_adapter, launch_dynamic

APP_NAME = "biodesign-vat-dynamic"
FUNCTION_NAME = "dynamic-job"

if modal is not None:
    app = modal.App(APP_NAME)
    image = (modal.Image.debian_slim(python_version="3.12")
             .pip_install("pydantic==2.12.5", "cryptography>=39", "rfc8785>=0.1", "pymerkle>=6.1")
             .add_local_python_source("runner", "vatlog"))

    @vat(result_adapter=dynamic_result_adapter)
    @app.function(name=FUNCTION_NAME, image=image, cpu=1.0, memory=2048, timeout=300, retries=0,
                  block_network=True, restrict_modal_access=True)
    def dynamic_job(execution_id: str, proposal: dict):
        return launch_dynamic(proposal)
else:  # pragma: no cover
    app = image = dynamic_job = None
