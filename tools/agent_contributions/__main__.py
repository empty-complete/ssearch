"""Owner-configured developer CLI. No issue input, fixture switch or command runner."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from .config import load_config, read_owner_json, require
from .contracts import ContributionAuthorization, ContributionError
from .workspace import create_workspace


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Create an explicitly authorized ssearch developer workspace")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--grant", required=True, type=Path)
    parser.add_argument("--parent", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        value = read_owner_json(args.grant)
        require(set(value) == set(ContributionAuthorization.__dataclass_fields__), "invalid_grant")
        require(isinstance(value["workspace_root"], str) and isinstance(value["expires_at"], str), "invalid_grant")
        try:
            value["expires_at"] = datetime.fromisoformat(value["expires_at"])
        except ValueError:
            raise ContributionError("invalid_expiry") from None
        value["workspace_root"] = Path(value["workspace_root"])
        result = create_workspace(config, ContributionAuthorization(**value), parent=args.parent,
                                  now=datetime.now(timezone.utc))
        print(json.dumps(dict(path=str(result.path), branch=result.branch, base_sha=result.base_sha)))
        return 0
    except ContributionError as exc:
        print(json.dumps(dict(error_code=exc.code)))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
