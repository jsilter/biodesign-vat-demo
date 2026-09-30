"""Run a real, offline fork/join and export evidence for the graph viewer."""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from vatlog import VatRecorder, vat
from vatlog.provenance import bundle_provenance
from vatlog.roles import Keyring
from vatlog.session_export import publish_recorded
from vatlog.verify import verify_bundle


@vat
def normalize(values):
    return sorted(set(values))


@vat
async def total(values):
    await asyncio.sleep(0)
    return sum(values)


@vat
def report(first, second):
    return {"first": first, "second": second, "combined": first + second}


@vat
def rejected(value):
    raise ValueError("Intentional demonstration failure")


async def experiment(recorder, values):
    normalized = await normalize.with_recorder(recorder).call.aio(values)
    left, right = await asyncio.gather(total.with_recorder(recorder).call.aio(normalized.ref()),
                                      total.with_recorder(recorder).call.aio(normalized.ref()))
    combined = await report.with_recorder(recorder).call.aio(left.ref(), right.ref())
    if combined.value["combined"] > 0:
        await rejected.with_recorder(recorder).call.aio(combined.ref('/combined'))
    return combined.value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path('.local/provenance-demo'))
    parser.add_argument('--values', type=int, nargs='+', default=[3, 1, 3, 5])
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    keys = Keyring.generate()
    with VatRecorder(args.out / 'journal', keys.recorder) as recorder:
        result = asyncio.run(experiment(recorder, args.values))
        bundle = publish_recorded(recorder, args.out, keys, str(uuid4()),
                                  prompt='Local arithmetic provenance demonstration')
    bundle.save(args.out / 'bundle.zip')
    keys.trust_bundle().save(args.out / 'trust.json')
    print(json.dumps(result))
    print(verify_bundle(bundle, keys.trust_bundle()).render())
    print(json.dumps(asdict(bundle_provenance(bundle, keys.trust_bundle())[0]), indent=2))


if __name__ == '__main__':
    main()
