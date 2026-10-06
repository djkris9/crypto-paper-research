"""Check reviewed source hashes and public checkpoint validity before running."""
import hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
def main():
    manifest=json.loads((ROOT/"release_manifest.json").read_text())
    for name,digest in manifest["files"].items():
        path=ROOT/name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise RuntimeError("Reviewed release changed: "+name)
    from bot.slow_state import read_checkpoint
    read_checkpoint(ROOT/"state/paper.json")
    print(json.dumps(dict(reviewed_release_verified=True,mode="paper",ai=False)))
if __name__=="__main__":main()
