"""Fingerprint prediction semantics independently of route-search scheduling."""

import ast
import hashlib
from pathlib import Path

# Aliases are added only after exact feature-equivalence tests against a frozen
# implementation. Any future prediction-code change gets a new fingerprint and
# cannot silently reuse an alias for the old feature implementation.
VERIFIED_COMPATIBILITY = {
    # Python 3.12 / 3.13 AST encodings of the verified indexed-history build.
    # Frozen reference: 98만 OD, 30 actual feature batches exactly equal;
    # no model weights, feature formulas, route or weather semantics changed.
    "ad7b48c9b990e127a2665f2e96a74789b99313d04f5bea701619d14c586d4e63": "ec2495ab3da889e9648a6352eef47f8429acdf18fba636d3e4f30db186c814c1",
    "7dd3913eb6d5b89fd2b946bd5833fca3926da942428faaec17b83b97ab2b8614": "ec2495ab3da889e9648a6352eef47f8429acdf18fba636d3e4f30db186c814c1",
}


def prediction_code_identity(folder=None):
    folder = Path(folder or Path(__file__).parent)
    digest = hashlib.sha256()
    for name in (
        "eta_service.py",
        "route_runtime.py",
        "kma_weather_forecast_provider.py",
        "history_lookup.py",
    ):
        tree = ast.parse((folder / name).read_text(encoding="utf-8-sig"))
        digest.update(name.encode())
        digest.update(ast.dump(tree, include_attributes=False).encode())
    tree = ast.parse((folder / "multimodal.py").read_text(encoding="utf-8-sig"))
    provider = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "V521BikeProvider"
    )
    digest.update(ast.dump(provider, include_attributes=False).encode())
    identity = digest.hexdigest()
    return VERIFIED_COMPATIBILITY.get(identity, identity)
