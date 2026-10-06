"""One finite paper cycle with a durable public checkpoint; no persistent laptop process."""
from pathlib import Path
import argparse,subprocess,sys,tempfile
ROOT=Path(__file__).resolve().parents[1]
def main():
    parser=argparse.ArgumentParser();parser.add_argument("--checkpoint",default="state/paper.json");args=parser.parse_args()
    checkpoint=(ROOT/args.checkpoint).resolve()
    if not checkpoint.is_relative_to(ROOT/"state"):raise RuntimeError("Checkpoint must stay in the dedicated public state folder")
    if not checkpoint.is_file():raise RuntimeError("Forward ledger missing; cannot start a new balance silently")
    with tempfile.TemporaryDirectory(prefix="paper-cycle-") as folder:
        result=subprocess.run([sys.executable,"-m","bot.slow_paper","--once","--db",str(Path(folder)/"ephemeral.sqlite"),"--checkpoint",str(checkpoint)],cwd=ROOT)
    raise SystemExit(result.returncode)
if __name__=="__main__":main()
