"""Write the offline shared-image set used by session-news responses."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from catalyst_data.ingestion.session_news import write_shared_images_file


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--media", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    payload = write_shared_images_file(args.archive, args.media, args.out)
    print(json.dumps({"path": str(args.out), "count": payload["count"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
