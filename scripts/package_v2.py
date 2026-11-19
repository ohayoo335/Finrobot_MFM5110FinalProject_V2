"""Create an explicit development handoff ZIP; never include local credentials or envs."""
import hashlib
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "Finrobot_MFM5110FinalProject_V2"
DENY = {".git", ".venv", ".cache", "__pycache__", ".ipynb_checkpoints", "report", "site"}
DENY_FILES = {"OAI_CONFIG_LIST", "config_api_keys", "PERSONAL_KEYS", ".env", "config.ini"}
EXTENSIONS = {".py", ".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".cmd", ".ipynb", ".js", ".sh", ".bat", ".ps1"}


def main():
    files = [p for p in ROOT.iterdir() if p.is_file() and p.name != "SHARE_MANIFEST.json" and (p.suffix in EXTENSIONS or p.name in {"LICENSE", "NOTICE", "VERSION", ".gitignore", ".gitattributes"})]
    for folder in ("finrobot_autogen", "tests", "scripts", "docs", "data/quant_v2", "artifacts/quant_v2",
                   "data/quant_generalization", "artifacts/quant_generalization",
                   "artifacts/factor_ideation", "artifacts/stability"):
        for path in (ROOT / folder).rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if any(part in DENY or part.endswith(".egg-info") for part in relative.parts) or path.name in DENY_FILES or path.name.startswith(".env"):
                continue
            allowed = path.suffix in EXTENSIONS or path.name in {"LICENSE", "NOTICE", ".gitignore", ".gitmodules"}
            if folder.startswith(("data/", "artifacts/")):
                allowed = allowed or path.suffix in {".csv", ".png"}
            if allowed:
                files.append(path)
    files = sorted(set(files))
    config = ROOT / "finrobot_autogen/OAI_CONFIG_LIST"
    keys = []
    if config.exists():
        keys = [row.get("api_key", "") for row in json.loads(config.read_text(encoding="utf-8"))
                if row.get("api_key") and not row["api_key"].startswith("REPLACE_ME")]
    token_pattern = re.compile(rb"(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{25,})")
    manifest = []
    for path in files:
        content = path.read_bytes()
        if any(key.encode() in content for key in keys) or token_pattern.search(content):
            raise ValueError("Credential-like content in: " + path.relative_to(ROOT).as_posix())
        manifest.append({"path": path.relative_to(ROOT).as_posix(), "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    manifest_text = json.dumps(manifest, indent=2) + "\n"
    (ROOT / "SHARE_MANIFEST.json").write_text(manifest_text, encoding="utf-8")
    archive = dist / (NAME + "_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".zip")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, NAME + "/" + path.relative_to(ROOT).as_posix())
        zf.writestr(NAME + "/SHARE_MANIFEST.json", manifest_text)
    summary = {"archive": archive.name, "files": len(files) + 1, "bytes": archive.stat().st_size,
               "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "known_keys_excluded": True}
    (dist / "package_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
