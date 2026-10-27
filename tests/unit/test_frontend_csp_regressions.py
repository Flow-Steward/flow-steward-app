from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_platform_kit_tokens_decode_bundled_data_url_before_fetch() -> None:
    source = (ROOT / "assets" / "platform-kit" / "platform-base.js").read_text()

    assert "tokenUrl.protocol === 'data:'" in source
    assert "decodeDataUrlText(tokenUrl.href)" in source
    assert "await fetch(tokenUrl)" in source
    assert source.index("tokenUrl.protocol === 'data:'") < source.index("await fetch(tokenUrl)")


def test_frontend_uses_jitless_zod_wrapper_for_csp() -> None:
    frontend_sources = ROOT / "assets" / "frontend" / "src"
    wrapper = (frontend_sources / "lib" / "validation" / "zod.ts").read_text()
    assert "z.config({ jitless: true })" in wrapper

    direct_imports = []
    for path in frontend_sources.rglob("*"):
        if path.suffix not in {".ts", ".tsx"}:
            continue
        if path == frontend_sources / "lib" / "validation" / "zod.ts":
            continue
        source = path.read_text()
        if 'from "zod"' in source or "from 'zod'" in source:
            direct_imports.append(str(path.relative_to(frontend_sources)))

    assert direct_imports == []
