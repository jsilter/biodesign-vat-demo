"""Actual global stacked decorators loaded by the SDK's container importer offline."""
import modal
from vatlog import vat

app = modal.App("vat-container-test")


@vat
@app.function(cpu=1, retries=0)
def square(value):
    return value * value


@vat
@app.function(cpu=1, retries=0)
async def async_square(value):
    return value * value
