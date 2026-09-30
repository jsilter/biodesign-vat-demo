"""Isolated ESMC-only Modal deployment for the BenchBB reference study."""

import modal

from runner import modal_app as workers
from runner.vat_adapter import scientific_result_adapter, tool_identity
from vatlog import vat


app = modal.App("biodesign-vat-benchbb-esmc")
weights = modal.Volume.from_name("biodesign-vat-benchbb-esmc-weights", create_if_missing=True)


@vat(
    descriptor=tool_identity("esmc").model_copy(update={"app_name": app.name}),
    result_adapter=scientific_result_adapter,
)
@app.function(
    image=workers.esm_image,
    gpu="L4",
    cpu=2,
    memory=16384,
    timeout=1800,
    startup_timeout=600,
    retries=0,
    max_containers=1,
    scaledown_window=2,
    volumes={"/jobs": workers.volume, "/weights": weights},
)
def esmc(id: str, request: dict) -> dict:
    return workers.execute("esmc", id, request)


@app.function(
    image=workers.recover_image,
    cpu=0.25,
    memory=512,
    timeout=60,
    retries=0,
    max_containers=1,
    scaledown_window=2,
    volumes={"/jobs": workers.volume},
)
def recover(id: str) -> dict:
    return workers.recover.local(id)
