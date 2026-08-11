# X-RAG Bilingual Post Translation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve every English X post while adding a free local Chinese translation to Markdown, RAG search, and the public dashboard, including a safe one-time backfill of existing posts and automatic translation in future daily collections.

**Architecture:** Add an optional bilingual translation record to the immutable post models, keep language detection/protection/enrichment independent from the Hugging Face model installer/runtime, and inject the enricher only into ingest and backfill services. Markdown remains authoritative, Chroma indexes both languages, and the dashboard receives only the optional public `text_zh` field. Model installation, backfill, and publication are separate fail-closed operations; none removes source Markdown or media.

**Tech Stack:** Python 3.11+, Typer, dataclasses, PyYAML, Transformers/PyTorch, Hugging Face Hub, SentencePiece, Chroma, vanilla JavaScript, Node test runner, pytest, Windows Task Scheduler, WSL.

---

## File map

**Create**

- `src/xrag/translation.py` — language detection, protected-span handling, translation reuse, and post enrichment.
- `src/xrag/translation_model.py` — verified model installation/manifest loading and offline Transformers inference.
- `tests/test_translation.py` — pure translation and enrichment behavior.
- `tests/test_translation_model.py` — model manifest, installer, local-only loading, and inference adapter tests.

**Modify**

- `src/xrag/models.py` — translation metadata and optional main/quoted Chinese text.
- `src/xrag/markdown_store.py` — bilingual Markdown render/read and metadata validation.
- `src/xrag/importers.py` — preserve optional translation fields during Markdown/YAML/JSON import.
- `src/xrag/config.py` — project-local translation model directory.
- `src/xrag/service.py` — translate collect/import writes, backfill existing posts, and verify no source paths disappear.
- `src/xrag/cli.py` — translation install/backfill commands and ingest-only translator wiring.
- `src/xrag/dashboard_export.py` — optional public `text_zh` field.
- `dashboard/assets/app.js` — bilingual snapshot validation and Chinese-first rendering.
- `dashboard/index.html` — machine-translation badge hook.
- `dashboard/assets/styles.css` — badge and bilingual dialog styles.
- `pyproject.toml` — direct runtime dependencies used by the model layer.
- `.gitignore` — ignore project-local model snapshots.
- `README.md` — install, backfill, offline behavior, schedule, and disk-use instructions.
- Existing tests under `tests/` and `dashboard/tests/` — integration and regression coverage.

## Task 1: Add bilingual post domain types and searchable text

**Files:**

- Modify: `src/xrag/models.py`
- Modify: `tests/test_models.py`

- [ ] **Step 1: Write the failing model tests**

Add tests proving that translation metadata is immutable, that both main and quoted translations are optional, and that search text preserves this exact order: main original, main Chinese, quoted original, quoted Chinese.

```python
from xrag.models import Post, QuotedPost, TranslationMetadata


def translation() -> TranslationMetadata:
    return TranslationMetadata(
        language="zh-CN",
        model_id="Helsinki-NLP/opus-mt-en-zh",
        revision="abc123",
        source_sha256="1" * 64,
        translated_at="2026-08-11T12:00:00Z",
    )


def test_searchable_text_contains_original_and_chinese_for_main_and_quote() -> None:
    post = Post(
        "1", "Ada", "Agent security matters.", "2026-08-11T00:00:00Z",
        "https://x.com/i/status/1",
        text_zh="智能体安全很重要。",
        translation_zh=translation(),
        quoted_post=QuotedPost(
            "2", "Bob", "Keep x402 safe.", "2026-08-10T00:00:00Z",
            "https://x.com/i/status/2",
            text_zh="确保 x402 安全。",
            translation_zh=translation(),
        ),
    )
    assert post.searchable_text == (
        "Agent security matters.\n\n智能体安全很重要。\n\n"
        "Keep x402 safe.\n\n确保 x402 安全。"
    )
```

- [ ] **Step 2: Run the focused test and observe RED**

Run: `.venv/bin/python -m pytest tests/test_models.py -q`

Expected: FAIL because `TranslationMetadata`, `text_zh`, and `translation_zh` do not exist.

- [ ] **Step 3: Implement the minimal immutable fields**

Add this type before `QuotedPost`, then add the two optional fields to both post types and include nonblank translated text in `searchable_text`.

```python
@dataclass(frozen=True)
class TranslationMetadata:
    language: str
    model_id: str
    revision: str
    source_sha256: str
    translated_at: str


@dataclass(frozen=True)
class QuotedPost:
    id: str
    author: str
    text: str
    created_at: str
    url: str
    media_urls: tuple[str, ...] = ()
    media_posters: tuple[str, ...] = ()
    text_zh: str = ""
    translation_zh: TranslationMetadata | None = None


@dataclass(frozen=True)
class Post:
    id: str
    author: str
    text: str
    created_at: str
    url: str
    bio: str = ""
    likes: int = 0
    views: int = 0
    media_urls: tuple[str, ...] = ()
    media_posters: tuple[str, ...] = ()
    quoted_post: QuotedPost | None = None
    local_media: tuple[LocalMedia, ...] = ()
    source_keywords: tuple[str, ...] = ()
    source_type: str = "opencli"
    text_zh: str = ""
    translation_zh: TranslationMetadata | None = None

    @property
    def searchable_text(self) -> str:
        parts = [self.text.strip(), self.text_zh.strip()]
        if self.quoted_post is not None:
            parts.extend(
                [self.quoted_post.text.strip(), self.quoted_post.text_zh.strip()]
            )
        return "\n\n".join(part for part in parts if part)
```

Place the new default fields after all non-default dataclass fields and preserve compatibility for positional construction used by existing tests.

- [ ] **Step 4: Run model and vector tests and observe GREEN**

Run: `.venv/bin/python -m pytest tests/test_models.py tests/test_vector_store.py -q`

Expected: PASS; the existing original-only searchable text test still passes.

- [ ] **Step 5: Commit the domain change**

```bash
git add src/xrag/models.py tests/test_models.py tests/test_vector_store.py
git commit -m "feat: add bilingual post fields"
```

## Task 2: Implement language detection, protected text, and translation reuse

**Files:**

- Create: `src/xrag/translation.py`
- Create: `tests/test_translation.py`

- [ ] **Step 1: Write failing pure-function tests**

Cover the 15-letter/60-percent rule, Chinese with short English terms, URLs, mentions, tags, cashtags, inline code, terminology, placeholder corruption, reuse by source hash/model revision, main/quoted batching, and one-part failure retaining original text.

```python
def test_language_detection_requires_english_dominance() -> None:
    assert needs_english_translation("Autonomous agents need stronger security.")
    assert not needs_english_translation("AI 与 RWA 是今天的热门方向")
    assert not needs_english_translation("short English")


def test_protected_text_round_trips_exact_public_tokens() -> None:
    source = "@alice says AI Agent uses x402 at https://example.com/a?q=1 #Agents $SOL `pay()`"
    protected = protect_text(source)
    assert protected.restore(protected.text) == source
    with pytest.raises(ValueError, match="protected translation spans"):
        protected.restore(protected.text.replace("XRAG0000", "XRAG9999"))


def test_enricher_reuses_matching_translation_without_model_call() -> None:
    existing = bilingual_post(source="Agents coordinate payments.", revision="abc123")
    engine = FakeEngine(result="不应调用")
    outcome = TranslationEnricher(engine, clock=fixed_clock).enrich(existing, existing)
    assert outcome.reused == 1
    assert outcome.translated == 0
    assert engine.calls == []
```

- [ ] **Step 2: Run the focused test and observe RED**

Run: `.venv/bin/python -m pytest tests/test_translation.py -q`

Expected: FAIL with `ModuleNotFoundError: xrag.translation`.

- [ ] **Step 3: Implement deterministic detection and protected spans**

Create these public interfaces and constants in `src/xrag/translation.py`:

```python
class TranslationEngine(Protocol):
    model_id: str
    revision: str
    def preflight(self) -> None: ...
    def translate_many(self, texts: Sequence[str]) -> list[str]: ...


@dataclass(frozen=True)
class ProtectedText:
    text: str
    replacements: tuple[tuple[str, str], ...]

    def restore(self, translated: str) -> str:
        result = translated
        positions = []
        for marker, value in self.replacements:
            if result.count(marker) != 1:
                raise ValueError("protected translation spans were not preserved")
            positions.append(result.index(marker))
            result = result.replace(marker, value)
        if positions != sorted(positions):
            raise ValueError("protected translation spans changed order")
        return result.strip()


def needs_english_translation(text: str) -> bool:
    visible = _PROTECTED_PATTERN.sub(" ", text)
    latin = len(re.findall(r"[A-Za-z]", visible))
    cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", visible))
    return latin >= 15 and latin / max(latin + cjk, 1) >= 0.60
```

`protect_text()` must enumerate matches left-to-right, replace them with unique `XRAG0000TOKEN` markers, and store exact `(marker, original)` pairs. Build `_PROTECTED_PATTERN` from URL, mention, hashtag, cashtag, inline-code, and an escaped longest-first terminology list.

- [ ] **Step 4: Implement enrichment and strict reuse**

Add immutable `TranslationOutcome(post, translated, reused, skipped, errors)` and implement `TranslationEnricher.enrich(post, existing)` with this exact behavior:

1. Call `engine.preflight()` before processing.
2. For main and quoted text independently, skip text that fails language detection.
3. Reuse an existing nonblank translation only when `language`, `model_id`, `revision`, and SHA-256 of the current original all match.
4. Protect all remaining originals, call one `translate_many()` batch, require equal input/output lengths and nonblank restored text, then attach new `TranslationMetadata` using the injected UTC clock.
5. If an individual restored output fails, retain that part without a translation and add a generic `TranslationFailure(owner, "translation failed")`; never include source or translated text in the failure.

Use `dataclasses.replace` to create new posts and quoted posts; do not mutate inputs.

- [ ] **Step 5: Run focused tests and observe GREEN**

Run: `.venv/bin/python -m pytest tests/test_translation.py tests/test_models.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the pure translation layer**

```bash
git add src/xrag/translation.py tests/test_translation.py
git commit -m "feat: add local translation enrichment"
```

## Task 3: Install and load a verified project-local model

**Files:**

- Create: `src/xrag/translation_model.py`
- Create: `tests/test_translation_model.py`
- Modify: `src/xrag/config.py`
- Modify: `tests/test_config.py`
- Modify: `pyproject.toml`
- Modify: `.gitignore`

- [ ] **Step 1: Write failing installer and manifest tests**

Use an injected fake Hugging Face API/downloader to create a disposable snapshot. Assert that installation pins the returned repository commit, hashes every regular file, writes `manifest.json` last, preserves an older valid snapshot, rejects symlinks/reparse points/path traversal/hash changes, and only cleans the exact staging directory created by the current call.

```python
def test_install_pins_revision_and_writes_verified_manifest(tmp_path: Path) -> None:
    root = tmp_path / "translation"
    result = install_translation_model(
        root,
        api=FakeApi(sha="a" * 40),
        downloader=fake_snapshot_download,
    )
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert result.revision == "a" * 40
    assert manifest["snapshot"] == "snapshots/" + "a" * 40
    assert manifest["files"]["config.json"] == sha256_bytes(b"{}")
    assert verify_translation_model(root) == result
```

Add a loader test that monkeypatches `AutoTokenizer` and `AutoModelForSeq2SeqLM` and proves both receive the verified local snapshot path with `local_files_only=True` and `trust_remote_code=False`.

- [ ] **Step 2: Run model tests and observe RED**

Run: `.venv/bin/python -m pytest tests/test_translation_model.py tests/test_config.py -q`

Expected: FAIL because the model module and `translation_model_dir` are absent.

- [ ] **Step 3: Implement manifest verification and atomic installation**

Create `InstalledTranslationModel(model_id, revision, snapshot_path, files)` and constants:

```python
MODEL_ID = "Helsinki-NLP/opus-mt-en-zh"
MANIFEST_VERSION = 1


@dataclass(frozen=True)
class InstalledTranslationModel:
    model_id: str
    revision: str
    snapshot_path: Path
    files: dict[str, str]


def _regular_file_without_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISREG(info.st_mode) and not (attributes & reparse)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_translation_model(root: Path) -> InstalledTranslationModel:
    root = Path(root).resolve()
    manifest_path = root / "manifest.json"
    if not _regular_file_without_reparse(manifest_path):
        raise RuntimeError("translation model manifest is invalid")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("translation model manifest is invalid") from error
    if not isinstance(manifest, dict) or set(manifest) != {
        "version", "model_id", "revision", "snapshot", "files"
    }:
        raise RuntimeError("translation model manifest is invalid")
    revision = manifest["revision"]
    expected_snapshot = f"snapshots/{revision}"
    if (
        manifest["version"] != MANIFEST_VERSION
        or manifest["model_id"] != MODEL_ID
        or not isinstance(revision, str)
        or re.fullmatch(r"[0-9a-f]{40}", revision) is None
        or manifest["snapshot"] != expected_snapshot
        or not isinstance(manifest["files"], dict)
        or not manifest["files"]
    ):
        raise RuntimeError("translation model manifest is invalid")
    snapshot = (root / Path(*expected_snapshot.split("/"))).resolve()
    if snapshot.parent != (root / "snapshots").resolve() or not snapshot.is_dir():
        raise RuntimeError("translation model snapshot is invalid")
    files = manifest["files"]
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in files.items()):
        raise RuntimeError("translation model file manifest is invalid")
    verified: dict[str, str] = {}
    for relative in sorted(files):
        expected_hash = files[relative]
        if (
            re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
        ):
            raise RuntimeError("translation model file manifest is invalid")
        parts = relative.split("/")
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise RuntimeError("translation model file path is invalid")
        candidate = snapshot.joinpath(*parts)
        if not _regular_file_without_reparse(candidate):
            raise RuntimeError("translation model file is invalid")
        actual_hash = _sha256_file(candidate)
        if actual_hash != expected_hash:
            raise RuntimeError("translation model file hash mismatch")
        verified[relative] = actual_hash
    return InstalledTranslationModel(
        model_id=MODEL_ID,
        revision=revision,
        snapshot_path=snapshot,
        files=verified,
    )
```

`install_translation_model()` must resolve `HfApi.model_info(MODEL_ID).sha`, create one random `.install-<uuid>` directory beneath the verified root, call `snapshot_download(repo_id=MODEL_ID, revision=sha, local_dir=staging)`, hash all regular non-link files, atomically rename staging to `snapshots/<sha>` when absent, then atomically replace only `manifest.json`. Do not remove an existing snapshot. On failure, remove only the exact staging directory allocated by this invocation after verifying its parent/name/type.

- [ ] **Step 4: Implement local-only deterministic inference**

Create `TransformersTranslationEngine` implementing the Task 2 protocol. `preflight()` calls `verify_translation_model`; first inference lazily imports torch/transformers, loads tokenizer/model from `snapshot_path`, selects CPU, sets evaluation mode, and never permits remote code or network lookup.

```python
encoded = tokenizer(
    list(texts), return_tensors="pt", padding=True, truncation=True,
    max_length=512,
)
with torch.inference_mode():
    generated = model.generate(
        **encoded, do_sample=False, num_beams=4, max_new_tokens=512,
    )
return tokenizer.batch_decode(generated, skip_special_tokens=True)
```

Reject an empty input element, output count mismatch, or blank output with a generic `RuntimeError("local translation failed")`.

- [ ] **Step 5: Add direct dependencies and ignored model path**

Add compatible direct ranges to `pyproject.toml` for `transformers`, `huggingface-hub`, `sentencepiece`, and `sacremoses`; retain existing dependency ranges. Add only `/data/models/` to `.gitignore`. Add `AppConfig.translation_model_dir` returning `root / "data" / "models" / "translation"`.

- [ ] **Step 6: Run focused tests and observe GREEN**

Run: `.venv/bin/python -m pytest tests/test_translation_model.py tests/test_config.py -q`

Expected: PASS without network access or a real model download.

- [ ] **Step 7: Commit the verified model layer**

```bash
git add src/xrag/translation_model.py tests/test_translation_model.py src/xrag/config.py tests/test_config.py pyproject.toml .gitignore
git commit -m "feat: manage verified local translation model"
```

## Task 4: Round-trip translations through Markdown and imports

**Files:**

- Modify: `src/xrag/markdown_store.py`
- Modify: `src/xrag/importers.py`
- Modify: `tests/test_markdown_store.py`
- Modify: `tests/test_importers.py`

- [ ] **Step 1: Write failing Markdown compatibility tests**

Add tests for original-only legacy files, bilingual main text, bilingual quoted text, metadata validation, malformed/duplicate translation markers, exact original preservation, and import of canonical bilingual Markdown.

```python
def test_bilingual_markdown_round_trip_keeps_original_and_translation(tmp_path: Path) -> None:
    store = MarkdownStore(tmp_path)
    source = bilingual_post()
    path = store.upsert(source)
    rendered = path.read_text(encoding="utf-8")
    assert "<!-- xrag:text:start -->\nAgent security matters.\n<!-- xrag:text:end -->" in rendered
    assert "## 中文翻译（机器翻译）" in rendered
    assert "<!-- xrag:text-zh:start -->\n智能体安全很重要。\n<!-- xrag:text-zh:end -->" in rendered
    assert store.read(path) == source
```

- [ ] **Step 2: Run focused tests and observe RED**

Run: `.venv/bin/python -m pytest tests/test_markdown_store.py tests/test_importers.py -q`

Expected: FAIL because translated fields are neither rendered nor parsed.

- [ ] **Step 3: Implement optional translation metadata mappings**

Add `_translation_to_mapping()` and `_translation_from_value()` with exact keys `language`, `model_id`, `revision`, `source_sha256`, and `translated_at`. Require `language == "zh-CN"`, a 64-lowercase-hex source hash, nonblank model/revision/timestamp strings, and reject bool/list/nested unexpected types. Use the mapping under top-level `translation_zh`; extend the quoted mapping with `text_zh` and `translation_zh`.

- [ ] **Step 4: Implement independent translation body markers**

Define `_TEXT_ZH_START` and `_TEXT_ZH_END`. Add `extract_body_translation(body, canonical=True)` returning `""` when both markers are absent and rejecting one-sided, duplicated, reversed, or blank marked translations. `_render_body()` appends the Chinese section only when `post.text_zh.strip()` and matching metadata are both present. Render quoted Chinese immediately after the quoted original using blockquote lines headed `> 中文翻译（机器翻译）：`.

Update `read()` and `_normalize_row()` to reject text/metadata half-pairs: translated text without metadata or metadata without translated text is invalid.

- [ ] **Step 5: Preserve bilingual fields through import**

For Markdown import, set `row["text_zh"] = extract_body_translation(...)`. For YAML/JSON, accept `text_zh` and `translation_zh`; use `_quoted_from_value()` for quoted bilingual data. Imported bilingual metadata must pass the same Markdown-store validation before a `Post` is created.

- [ ] **Step 6: Run focused and regression tests and observe GREEN**

Run: `.venv/bin/python -m pytest tests/test_markdown_store.py tests/test_importers.py tests/test_offline_flow.py tests/test_opencli.py -q`

Expected: PASS; old Markdown fixtures remain readable and original text assertions remain unchanged.

- [ ] **Step 7: Commit storage compatibility**

```bash
git add src/xrag/markdown_store.py src/xrag/importers.py tests/test_markdown_store.py tests/test_importers.py
git commit -m "feat: persist bilingual markdown"
```

## Task 5: Translate collection/import writes and backfill existing posts

**Files:**

- Modify: `src/xrag/service.py`
- Modify: `tests/test_service.py`
- Modify: `tests/test_atomic_rebuild.py`
- Modify: `tests/test_vector_lifecycle.py`

- [ ] **Step 1: Write failing ingest tests**

Inject a fake `TranslationEnricher` and assert preflight happens before `opencli.search`, matching existing translations are reused, new main/quoted translations reach Markdown and `index_post`, Chinese posts are skipped, and individual failures store/index the original while adding `translation_errors`.

```python
def test_collect_preflights_translation_before_search_and_indexes_bilingual_post(tmp_path: Path) -> None:
    events: list[str] = []
    enricher = FakeEnricher(events, translated_post=bilingual_post())
    opencli = FakeOpenCLI(events)
    service = make_service(tmp_path, opencli=opencli, translation=enricher)
    counts = service.collect("Agent Security")
    assert events[:2] == ["translation-preflight", "opencli-search"]
    assert counts["translated"] == 1
    assert service.markdown.get("1").text_zh == "智能体安全很重要。"
    assert service.vectors.posts[0].text_zh == "智能体安全很重要。"
```

- [ ] **Step 2: Write failing backfill/non-deletion tests**

Create old Markdown plus media sentinels, translate one post, skip one Chinese post, fail one post, and assert: no old relative path disappears, only the translated Markdown changes, media bytes are identical, the atomic rebuild indexes current bilingual posts, and counts are exact.

- [ ] **Step 3: Run service tests and observe RED**

Run: `.venv/bin/python -m pytest tests/test_service.py tests/test_atomic_rebuild.py tests/test_vector_lifecycle.py -q`

Expected: FAIL because `XragService` has no translation dependency or backfill method.

- [ ] **Step 4: Integrate enrichment into collect and import**

Add optional constructor argument `translation: TranslationEnricher | None`. For ingest services it is required by the CLI factory; legacy tests and read-only services may leave it `None`. At the start of `collect()` and `import_path()`, call `preflight()` before OpenCLI/file processing. After media archival and before Markdown upsert:

```python
existing = self.markdown.get(self._post_id(item))
outcome = self.translation.enrich(item, existing)
item = outcome.post
counts["translated"] += outcome.translated
counts["translation_reused"] += outcome.reused
counts["translation_skipped"] += outcome.skipped
counts["translation_errors"] += len(outcome.errors)
for failure in outcome.errors:
    self._log_error(
        "translation", f"{self._post_id(item)}:{failure.owner}",
        RuntimeError(failure.reason), fixed_message="translation failed",
    )
```

Initialize the four translation counters in collect/import results. Do not include source or translated text in errors.

- [ ] **Step 5: Implement locked backfill and atomic index rebuild**

Add `translate_all()` that preflights before acquiring/writing data, then under the writer lock snapshots relative regular-file paths and SHA-256 values beneath `data/markdown` and `data/media`, enriches every readable Markdown post, atomically upserts only posts whose translated representation changed, and invokes `_rebuild_atomic()` once. Re-scan afterward and raise `RuntimeError("translation backfill removed source data")` if any original path is absent; report changed byte hashes without treating expected translated Markdown updates as deletions.

Return:

```python
{
    "scanned": scanned,
    "translated": translated,
    "reused": reused,
    "skipped": skipped,
    "errors": errors,
    "updated_documents": updated_documents,
    "missing_source_files": 0,
    "chunks": rebuild_counts["chunks"],
}
```

If rebuild returns errors, raise a generic runtime error after writing the last-run record; the stable pre-backfill index remains intact via the existing staging/swap behavior.

- [ ] **Step 6: Run focused tests and observe GREEN**

Run: `.venv/bin/python -m pytest tests/test_service.py tests/test_atomic_rebuild.py tests/test_vector_lifecycle.py tests/test_vector_store.py -q`

Expected: PASS.

- [ ] **Step 7: Commit service integration**

```bash
git add src/xrag/service.py tests/test_service.py tests/test_atomic_rebuild.py tests/test_vector_lifecycle.py
git commit -m "feat: translate ingest and backfill posts"
```

## Task 6: Add CLI install/backfill commands and fail-closed factory wiring

**Files:**

- Modify: `src/xrag/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Test exact commands `translation install` and `translation backfill`, JSON path encoding, generic secret redaction, and service selection. Prove collect/import/dashboard update build a translation-enabled ingest service, while search/status/rebuild/dashboard build/dashboard publish do not load or require the translation model.

```python
def test_translation_install_only_installs_model(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(cli, "install_translation_model", lambda path: calls.append(path) or fake_install())
    result = runner.invoke(cli.app, ["--root", str(tmp_path), "translation", "install"])
    assert result.exit_code == 0
    assert calls == [tmp_path.resolve() / "data" / "models" / "translation"]


def test_dashboard_update_uses_translation_enabled_ingest_service(monkeypatch, tmp_path: Path) -> None:
    events = []
    monkeypatch.setattr(cli, "build_ingest_service", lambda root: FakeService(events))
    monkeypatch.setattr(cli, "build_dashboard", lambda root: FakeDashboard(events))
    result = runner.invoke(cli.app, ["--root", str(tmp_path), "dashboard", "update", "--no-publish"])
    assert result.exit_code == 0
    assert events == ["collect-with-translation", "build"]
```

- [ ] **Step 2: Run CLI tests and observe RED**

Run: `.venv/bin/python -m pytest tests/test_cli.py -q`

Expected: FAIL because the command group and ingest factory are missing.

- [ ] **Step 3: Implement explicit factories**

Keep `build_service(root)` read-only/model-independent. Add `build_translation_enricher(config)` using `TransformersTranslationEngine(config.translation_model_dir)` and `build_ingest_service(root)` that injects it into `XragService`. Use the ingest factory only for collect, import, dashboard update, and translation backfill. Preserve existing vector/rebuild factories and media store.

- [ ] **Step 4: Implement the translation Typer group**

```python
translation_app = typer.Typer(no_args_is_help=True)
app.add_typer(translation_app, name="translation")


@translation_app.command("install")
def translation_install(ctx: typer.Context) -> None:
    config = load_config(ctx.obj)
    _print_json(_run(lambda: install_translation_model(config.translation_model_dir)))


@translation_app.command("backfill")
def translation_backfill(ctx: typer.Context) -> None:
    _print_json(_run(lambda: build_ingest_service(ctx.obj).translate_all()))
```

Make the install result JSON-serializable without exposing absolute snapshot paths; output model ID, revision, and verified file count. Extend `_summary()` with translated/reused/skipped/translation-error counts while retaining the existing found/stored/chunks/errors fields.

- [ ] **Step 5: Run CLI tests and observe GREEN**

Run: `.venv/bin/python -m pytest tests/test_cli.py tests/test_offline_flow.py -q`

Expected: PASS; read-only commands work with no model directory.

- [ ] **Step 6: Commit CLI wiring**

```bash
git add src/xrag/cli.py tests/test_cli.py
git commit -m "feat: add translation management commands"
```

## Task 7: Export the optional Chinese dashboard field

**Files:**

- Modify: `src/xrag/dashboard_export.py`
- Modify: `tests/test_dashboard_export.py`
- Modify: `tests/test_public_content.py`

- [ ] **Step 1: Write failing exporter tests**

Assert `_public_post()` includes nonblank `text_zh`, omits the key for untranslated posts, scans the translated value for credentials/local paths, and keeps summary/topic/media values unchanged.

```python
def test_dashboard_exports_only_nonblank_chinese_translation(builder_fixture) -> None:
    translated = builder_fixture(post=bilingual_post()).build_payload()["posts"][0]
    untranslated = builder_fixture(post=english_post()).build_payload()["posts"][0]
    assert translated["text_zh"] == "智能体安全很重要。"
    assert "text_zh" not in untranslated
```

- [ ] **Step 2: Run exporter tests and observe RED**

Run: `.venv/bin/python -m pytest tests/test_dashboard_export.py tests/test_public_content.py -q`

Expected: FAIL because `text_zh` is not exported.

- [ ] **Step 3: Implement the minimal public field**

Build the existing post dictionary first, then conditionally set `payload["text_zh"] = post.text_zh.strip()` only when nonblank and translation metadata exists. Keep snapshot `version` at `1` because the field is optional and old clients can ignore it. Continue serializing with `ensure_ascii=False` and running `assert_public_content()` over the complete JSON before any output write.

- [ ] **Step 4: Run exporter/security tests and observe GREEN**

Run: `.venv/bin/python -m pytest tests/test_dashboard_export.py tests/test_public_content.py tests/test_dashboard_publish.py -q`

Expected: PASS.

- [ ] **Step 5: Commit public schema support**

```bash
git add src/xrag/dashboard_export.py tests/test_dashboard_export.py tests/test_public_content.py
git commit -m "feat: export dashboard translations"
```

## Task 8: Render Chinese-first cards and bilingual details

**Files:**

- Modify: `dashboard/assets/app.js`
- Modify: `dashboard/index.html`
- Modify: `dashboard/assets/styles.css`
- Modify: `dashboard/tests/app.test.mjs`
- Modify: `tests/test_dashboard_assets.py`

- [ ] **Step 1: Write failing JavaScript contract tests**

Export `displayText(post)` and test translated/untranslated behavior. Extend snapshot cases so `text_zh` accepts only a trimmed nonblank string when present, rejects HTML-like unsafe credential/path content through the existing public contract fixtures, and does not change aggregate validation.

```javascript
test("displayText prefers a nonblank Chinese translation", () => {
  assert.equal(displayText({text: "Agent security", text_zh: "智能体安全"}), "智能体安全");
  assert.equal(displayText({text: "Agent security"}), "Agent security");
});

test("isValidSnapshot rejects blank or non-string translations", () => {
  for (const text_zh of ["", "   ", null, 42, []]) {
    const payload = validSnapshot();
    payload.posts[0].text_zh = text_zh;
    assert.equal(isValidSnapshot(payload), false);
  }
});
```

- [ ] **Step 2: Write failing static DOM/style tests**

Require a `.translation-badge` element in the post template, hidden-state CSS, `.dialog-language-heading`, and `.dialog-original` styling that wraps long URLs/text without horizontal overflow.

- [ ] **Step 3: Run frontend tests and observe RED**

Run: `node --test dashboard/tests/app.test.mjs`

Run: `.venv/bin/python -m pytest tests/test_dashboard_assets.py -q`

Expected: FAIL because translated validation/rendering/hooks do not exist.

- [ ] **Step 4: Implement validation and Chinese-first card rendering**

In `validPost`, accept absent `text_zh`; when present require `typeof === "string"`, exact trim equality, and nonblank. Add:

```javascript
export function displayText(post) {
  return typeof post.text_zh === "string" && post.text_zh.trim()
    ? post.text_zh
    : post.text;
}
```

Use `displayText(post)` for lead/card excerpts. Set `.translation-badge.hidden = !post.text_zh` and show “机器翻译” only when translated. Continue assigning all post data through `textContent`, never `innerHTML`.

- [ ] **Step 5: Implement bilingual detail rendering**

When `text_zh` exists, append a “中文译文（机器翻译）” heading and translated paragraph, then an “英文原文” heading and original paragraph. Without `text_zh`, append one original paragraph. Keep metadata, media gallery, keywords, source link, focus restoration, and dialog containment unchanged.

Add pale neutral badge colors, readable contrast, `overflow-wrap: anywhere`, and responsive vertical spacing. Add `.translation-badge[hidden] { display: none; }` so author CSS cannot override the hidden attribute.

- [ ] **Step 6: Run frontend tests and observe GREEN**

Run: `node --test dashboard/tests/app.test.mjs`

Run: `node --check dashboard/assets/app.js`

Run: `.venv/bin/python -m pytest tests/test_dashboard_assets.py tests/test_dashboard_export.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the bilingual interface**

```bash
git add dashboard/assets/app.js dashboard/index.html dashboard/assets/styles.css dashboard/tests/app.test.mjs tests/test_dashboard_assets.py
git commit -m "feat: show dashboard translations"
```

## Task 9: Document and lock the daily offline translation contract

**Files:**

- Modify: `README.md`
- Modify: `tests/test_scheduler_scripts.py`

- [ ] **Step 1: Write failing scheduler/documentation tests**

Assert the daily runner still invokes exactly one `dashboard update --no-publish`, contains no `translation install`, and README states that the model must be installed before enabling/running the task, daily translation is local-only, model upgrades/deletion are manual, and failed model verification leaves the live snapshot unchanged.

```python
def test_daily_runner_never_downloads_or_upgrades_translation_model() -> None:
    script = RUNNER.read_text(encoding="utf-8")
    assert script.count("dashboard update --no-publish") == 1
    assert "translation install" not in script
    assert "snapshot_download" not in script
```

- [ ] **Step 2: Run scheduler tests and observe RED**

Run: `.venv/bin/python -m pytest tests/test_scheduler_scripts.py -q`

Expected: FAIL on missing bilingual README contract assertions.

- [ ] **Step 3: Update operational documentation**

Add exact commands for model installation and backfill, expected model location/disk growth, translation counters, Chinese search, card/detail behavior, machine-translation disclaimer, failure recovery, backup advice, and the unchanged 10:00 sequence. State explicitly that installation/backfill may add or atomically update project files but never deletes source Markdown/media or computer files, and that historical model snapshots are not automatically pruned.

- [ ] **Step 4: Run scheduler/docs tests and observe GREEN**

Run: `.venv/bin/python -m pytest tests/test_scheduler_scripts.py -q`

Run: `bash -n scripts/run-daily.sh`

Expected: PASS.

- [ ] **Step 5: Commit documentation and schedule contract**

```bash
git add README.md tests/test_scheduler_scripts.py
git commit -m "docs: explain offline post translation"
```

## Task 10: Verify, install, backfill, publish, and prove no deletion

**Files:**

- No production-code edits expected.
- Runtime writes are limited to `data/models/translation/`, translated `data/markdown/*.md`, the rebuild staging/index path, `data/dashboard-site/`, `logs/`, and the existing dedicated Pages worktree.

- [ ] **Step 1: Run the complete pre-download verification suite**

Run:

```bash
.venv/bin/python -m pytest -q
node --test dashboard/tests/app.test.mjs
node --check dashboard/assets/app.js
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m pip check
bash -n scripts/run-daily.sh
git diff --check
git status --short
```

Expected: all tests/checks exit `0`; only intentional committed changes exist.

- [ ] **Step 2: Record a non-destructive baseline**

In PowerShell, create a uniquely named manifest in `%TEMP%` containing relative path, length, and SHA-256 for every regular file under `data/markdown` and `data/media`. Also record current Markdown/media counts, current feature HEAD, current remote `gh-pages` SHA, and Task Scheduler state. Do not change, move, or delete any source file.

- [ ] **Step 3: Install and verify the real local model**

Run inside the project WSL environment:

```bash
.venv/bin/xrag --root . translation install
```

Expected: JSON reports model ID `Helsinki-NLP/opus-mt-en-zh`, a 40-hex revision, and a positive verified file count. Re-run the command and require idempotent reuse of the same verified snapshot. Confirm `git status --short` does not show model files.

- [ ] **Step 4: Run the real offline translation smoke test**

Disconnect the translator from network lookup by loading only through `verify_translation_model()` and `TransformersTranslationEngine`. Translate fixed strings containing `AI Agent`, `x402`, `@alice`, `$SOL`, and an HTTPS URL. Require nonblank CJK output and byte-exact protected spans. No post body is sent to a network service.

- [ ] **Step 5: Backfill existing posts**

Run:

```bash
.venv/bin/xrag --root . translation backfill
```

Expected: positive `scanned`, `missing_source_files: 0`, internally consistent translated/reused/skipped/error totals, and a successful atomic Chroma rebuild. Re-run and require `updated_documents: 0` except entries whose source/model metadata genuinely changed.

- [ ] **Step 6: Compare baseline and prove no deletion**

Recompute the source manifest. Require every pre-backfill Markdown/media relative path still exists; require every pre-existing media SHA-256 unchanged; report exactly which Markdown files changed due to added translations. Verify no files outside the authorized project runtime paths were created, modified, moved, or deleted by the commands.

- [ ] **Step 7: Verify bilingual RAG and local dashboard**

Run a Chinese semantic query for a translated English fixture/topic and confirm the hit shows the original X URL/Markdown path. Build the dashboard, validate `latest.json` with the real frontend `isValidSnapshot`, confirm translated posts have both `text` and `text_zh`, and run the public-content scanner on every generated text file.

```bash
.venv/bin/xrag --root . search "智能体安全" --top 5
.venv/bin/xrag --root . dashboard build
```

- [ ] **Step 8: Browser-smoke the bilingual UI**

Serve `data/dashboard-site/` locally. At desktop and 390px widths verify Chinese-first lead/cards, machine-translation badge visibility, complete Chinese/English detail sections, untranslated fallback, images, filters, sorting, dialog focus, no horizontal overflow, and zero console errors.

- [ ] **Step 9: Run one real scheduled update and publish**

Start only the existing `X-RAG Daily Collection` task; do not register a duplicate. Poll until it leaves `Running`, require `LastTaskResult = 0`, and inspect the scheduler log for collection translation counters, dashboard build, and `{"changed": true|false, "branch": "gh-pages"}`. Confirm next run remains 10:00 Asia/Singapore.

- [ ] **Step 10: Verify live delivery and remote SHAs**

Require HTTP 200 for the site and `data/latest.json`, byte-identical local/live JSON SHA-256, a valid bilingual frontend snapshot, safe media URLs, and no public credential/local-path findings. Push the feature branch, verify remote SHA equals local HEAD, and independently review the full design-to-HEAD diff for Critical/Important issues before integration.

---

## Plan self-review result

- Spec coverage: local-only model, pinned/verified installation, original preservation, main/quoted translation, Markdown/RAG/dashboard flow, backfill, automatic daily use, failure handling, testing, and non-deletion proof are each mapped to a task.
- Placeholder scan: no deferred implementation markers or unspecified error-handling steps remain.
- Type consistency: `TranslationMetadata`, `TranslationEngine`, `TranslationEnricher`, `TranslationOutcome`, `TransformersTranslationEngine`, `translation_model_dir`, `text_zh`, `translation_zh`, `install_translation_model()`, `verify_translation_model()`, and `translate_all()` use the same names throughout.
