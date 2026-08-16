"""Offline documentation and trigger-contract checks."""
from __future__ import annotations

from smoke_support import *

def test_skill_document_contract():
    """锁定易漂移的章节、引用和特殊集硬规则。"""
    skill_path = ROOT / "SKILL.md"
    reference_paths = sorted((ROOT / "references").glob("*.md"))
    docs = [skill_path, *reference_paths]
    texts = {path: path.read_text(encoding="utf-8") for path in docs}
    skill_text = texts[skill_path]
    workflow_text = texts[ROOT / "references" / "workflow.md"]
    metadata_text = texts[ROOT / "references" / "metadata-rules.md"]
    optional_text = texts[ROOT / "references" / "optional-tools.md"]
    artwork_text = texts[ROOT / "references" / "artwork-library.md"]
    plan_text = texts[ROOT / "references" / "plan-contract.md"]
    incremental_text = texts[ROOT / "references" / "incremental.md"]
    all_text = "\n".join(texts.values())
    routing_start = skill_text.index("## 1. 渐进式引用路由")
    routing_end = skill_text.index("完整刮削通常会依次读取全部相关 references")
    routing_lines = skill_text[routing_start:routing_end].splitlines()
    trigger_cases = json.loads(
        (ROOT / "tests" / "trigger_cases.json").read_text(encoding="utf-8")
    )

    assert "如果变更集只包含人类发布资料" in skill_text
    assert "则不运行测试" in skill_text
    assert "单元独占片源根时必须直接用该根作为 `output_dir`" in skill_text
    assert "`output_dir` 末级目录恰为 `meta` 或 `metadata` 时必须拒绝 plan" in skill_text
    assert "直接把该片源根作为 `output_dir`" in artwork_text
    assert "不要只为这些元数据再建立一层 `meta` / `metadata` 子目录" in artwork_text
    assert "禁止只为 `tvshow.nfo` / `movie.nfo` 和作品级图片额外创建" in plan_text
    assert "`output_dir` 末级目录恰为 `meta` 或 `metadata` 时，dry-run 和实际落盘都必须拒绝" in plan_text
    frontmatter = skill_text.split("---", 2)[1]
    frontmatter_keys = re.findall(r"^([a-zA-Z0-9_-]+):", frontmatter, flags=re.MULTILINE)
    assert frontmatter_keys == ["name", "description"], frontmatter_keys
    assert "未明确为动画时不触发" in frontmatter
    assert len(trigger_cases["should_trigger"]) >= 3
    assert [case["prompt"] for case in trigger_cases["should_not_trigger"]] == [
        "电影刮削", "电视剧刮削", "剧集刮削",
    ]

    required_references = {
        "references/workflow.md",
        "references/metadata-rules.md",
        "references/special-rules.md",
        "references/artwork-library.md",
        "references/plan-contract.md",
    }
    for reference in required_references:
        assert reference in skill_text, f"SKILL 缺少直接路由: {reference}"
    assert any(
        "首次配置、扫描、ffprobe" in line
        and "workflow.md" in line
        and "incremental.md" not in line
        and "optional-tools.md" not in line
        for line in routing_lines
    )
    assert any("已有成果的少量维护" in line and "incremental.md" in line for line in routing_lines)
    assert any("用户明确要求音频指纹" in line and "optional-tools.md" in line for line in routing_lines)
    for path in reference_paths:
        if len(texts[path].splitlines()) > 100:
            assert "## 目录" in "\n".join(texts[path].splitlines()[:30]), f"长 reference 缺目录: {path.name}"

    assert "按标题/内容判断重要度和系列" in all_text
    assert "special_order" in all_text
    assert "不自动替 Agent 重排" in all_text
    assert "只保留为匹配证据" in all_text
    assert "只查 Bangumi 即停止、或没有记账，均视为审查失败" in all_text
    assert "Bangumi 对应不明 ≠ 证据链已穷尽" in metadata_text
    assert "song_evidence" in skill_text and "song_evidence" in plan_text
    assert "找不到独立候选时完全不生成 `Specials/poster.jpg`" in all_text
    assert "visible_text_role" in artwork_text
    assert "primary_title_prominence" in artwork_text
    assert "Specials/poster.jpg` 必须与作品/季度主海报使用不同" in all_text
    assert "单张 `320×480`" in skill_text
    assert "每组最多 **5** 张" in artwork_text
    assert "Agent 必须在同一轮以原始 detail 查看全部 sheet" in artwork_text
    assert "anime-scraper-artwork-review-v1" in plan_text

    step_zero = skill_text.index("## Step 0：环境与配置检查")
    stage_gates = skill_text.index("## 2. 四阶段执行门")
    assert step_zero < stage_gates
    for field in (
        "anidb.http.client",
        "anidb.http.clientver",
        "bangumi.access_token",
        "bangumi.user_agent",
        "tmdb.access_token",
        "tmdb.api_key",
    ):
        assert f"`{field}`" in workflow_text, f"Step 0 缺少配置检查: {field}"
    assert "anidb.http.enabled" not in all_text
    config_path = ROOT / "tests" / "fixtures" / "config.test.json"
    assert config_path == TEST_CONFIG_PATH
    config = load_test_config()
    assert "enabled" not in config["anidb"]["http"]
    assert config["anidb"]["http"]["client"] == ""
    assert config["artwork"]["multimodal_review"] is False
    assert config["artwork"]["artwork_cache"] is False
    assert config["library"]["hardlinks"]["enabled"] is True
    assert config["library"]["hardlinks"]["root"] == ""
    assert "link_root" not in config["paths"]
    assert "api_key" not in config["tmdb"], "config 应使用 access_token 承载读访问令牌"
    assert config["paths"]["source_root"] == ""
    assert config["bangumi"]["access_token"] == ""
    assert config["bangumi"]["user_agent"] == ""
    assert config["tmdb"]["access_token"] == ""
    assert "names" not in config
    assert not (ROOT / "config.example.json").exists()
    assert "config.example.json" not in all_text
    assert "--init-config" not in all_text
    assert "ANIME_SCRAPER_CONFIG" not in all_text
    assert '"plan_schema": "anime-scraper-plan"' in plan_text
    assert f'"plan_schema": "{scrape.PLAN_SCHEMA}-' not in plan_text
    assert (ROOT / "scripts" / "bangumi_search.py").is_file()
    assert "scripts/bangumi_search.py" in skill_text
    assert "bangumi_search.py" in workflow_text
    assert (ROOT / "scripts" / "metadata_snapshot.py").is_file()
    assert "scripts/metadata_snapshot.py" in skill_text
    assert "metadata_snapshot.py" in workflow_text
    assert "禁止为一次刮削另写临时 Python/PowerShell 脚本" in workflow_text
    assert "anime-scraper-metadata-v1" in workflow_text
    authorization_ambiguities = ("单独授权", "单独写入确认", "另行授权")
    for phrase in authorization_ambiguities:
        assert phrase not in skill_text, f"主刮削契约不应要求额外阶段授权: {phrase}"
    assert "不得仅因进入落盘阶段再次索要确认" in skill_text
    assert "无需因阶段切换再次征求用户确认" in skill_text
    assert "config.artwork.multimodal_review" in skill_text
    assert "config.artwork.artwork_cache" in skill_text
    assert "artwork.artwork_cache" in workflow_text
    assert "最终回复必须逐字报告该缓存目录" in skill_text
    assert "### 最终显性输出" in skill_text
    for output_field in ("作品/单元名称", "按规则略过清单", "待人工清单",
                         "candidate_id", "新 URL", "cache_path"):
        assert output_field in skill_text, output_field
    assert "- 状态：成功、部分完成或失败" not in skill_text
    assert "跳过项、空 plot 数" not in skill_text
    assert "- 规模：正片、Specials/extras、空 plot 数" not in skill_text
    assert "- 验证：sorttitle、plot、staff" not in skill_text
    assert "不要求固定标题、表格、分隔线或其他排版" in skill_text
    assert "只记录在内部验收产物中，不进入最终回复" in skill_text
    assert "当前 Agent 不能可靠查看图片时必须停止" in artwork_text
    assert "必须从最终 plan/report 读取后原样输出" in artwork_text
    assert "当 `artwork_review.original_cache_dir` 存在时" in plan_text
    assert "pHash/aHash/wHash 去重" in artwork_text
    assert "始终先执行" in artwork_text
    assert "--step-zero-status" in skill_text
    assert "Step 0：首次本机预检" in workflow_text
    assert "禁止输出 token、API key 或完整配置内容" in workflow_text
    assert "立即停止，不得开始扫描或联网请求" in workflow_text
    assert "把命令实际返回的绝对路径记为 `<config_path>`" in workflow_text
    assert "请编辑配置文件：<config_path>" in workflow_text
    assert "公开的根目录 `config.json` 只保留空值、空对象和默认选项" in workflow_text
    assert "需要填写或修正：<missing_fields>" in workflow_text
    assert "填写完成后请回复“已填写”" in workflow_text
    assert "必须返回 skill 根目录下的 `config.json`" in workflow_text
    assert "`cache/` API 缓存随 skill 目录一起迁移" in workflow_text
    assert "相对路径也以 skill 根目录为基准" in workflow_text
    assert "Python 虚拟环境位于 `<skill-root>/.runtime/venvs/`" in workflow_text
    assert "pip 下载缓存位于 `<skill-root>/.runtime/pip-cache/`" in workflow_text
    assert "删除 skill 目录时一并删除" in workflow_text
    assert "`.runtime/` 已被 `.gitignore` 排除且不得放进迁移包" in workflow_text
    assert "%APPDATA%/anime-scraper/" not in all_text

    headings = re.findall(r"^#### (4\.3\.\d+)\b", skill_text, flags=re.MULTILINE)
    assert headings == [f"4.3.{index}" for index in range(1, 8)], headings

    references = set(re.findall(r"references/[A-Za-z0-9._/-]+\.md", all_text))
    assert references, "文档应至少引用一份 references/*.md"
    for reference in references:
        assert (ROOT / reference).is_file(), f"引用文件不存在: {reference}"

    assert not (ROOT / "references" / "changelog.md").exists()
    assert not (ROOT / "scripts" / "migrate_flat.py").exists()
    assert not (ROOT / "scripts" / "migrate_library.py").exists()
    assert "migrate_library" not in all_text
    assert "changelog.md" not in all_text.lower()
    for removed_contract in (
        "--change-set", "--cache-dir", "cache_label", "legacy_missing",
        "anime-scraper-library-migration-v1", "兼容旧流程",
    ):
        assert removed_contract not in all_text, removed_contract
    link_library_source = (ROOT / "scripts" / "link_library.py").read_text(encoding="utf-8")
    assert "preview_library_migration" not in link_library_source
    assert "apply_library_migration" not in link_library_source
    assert not re.search(r"(?:§\s*)?4\.3[a-z]\b", all_text, flags=re.IGNORECASE)

    always_skip_short = re.compile(
        r"(?:所有|全部).{0,12}(?:<\s*60|小于\s*60|60\s*秒以下).{0,20}(?:跳过|略过)"
        r"|(?:<\s*60|小于\s*60|60\s*秒以下).{0,20}(?:一律|总是|全部都).{0,16}(?:跳过|略过)"
    )
    assert not always_skip_short.search(all_text)
    assert "所有前置匹配方法" in skill_text and "全部失败" in skill_text

    missing_credit_skip = re.compile(
        r"AniDB.{0,20}(?:没有|无|缺少).{0,20}(?:OP/ED|OP|ED).{0,20}(?<!不)(?:跳过|略过)"
    )
    assert not missing_credit_skip.search(all_text)
    assert "不跳过 NCOP/NCED" in all_text
    assert "`tmdb_match_status`" in skill_text and "`unknown`" in skill_text
    assert "未成功检查" in all_text and "不得伪造 `not_found`" in all_text
    assert "省略 `<plot>`" in skill_text
    assert "plot_evidence" in all_text
    for field in ("bangumi_zh", "tmdb_zh", "bangumi_ja", "tmdb_en"):
        assert field in plan_text, f"plot_evidence 缺少来源字段: {field}"
    assert "dry-run 先留报告再拒绝" in plan_text
    assert "`library.hardlinks.enabled=true`" in skill_text
    assert "`library.hardlinks.enabled=false`" in skill_text
    assert ("只有用户本次显式要求不建立硬链接" in skill_text
            or "只有用户本次明确要求不建硬链接" in skill_text)
    assert "--no-" + "hardlinks" in skill_text
    assert "完整 manifest" in skill_text and "完整 dry-run 报告" in skill_text
    assert "--report-file" in skill_text and "--verbose" in all_text
    assert ("最多保留 20 位唯一声优" in all_text
            or "最多 20 位唯一声优" in all_text)
    assert "声优卡位于 crew 前" in all_text
    assert "staff_status" in all_text
    assert "staff_note" in all_text
    assert "只有无干净类型的职位进入 `staff_note`" in metadata_text
    assert "staff_note 中的可映射职位标签" in plan_text
    assert "追加到 `<plot>` 末尾" in plan_text
    assert "show_plot" in (ROOT / "scripts" / "scrape.py").read_text(encoding="utf-8")
    assert "repair_staff.py" in all_text
    assert "增量变更模式" in skill_text
    assert "目标白名单" in incremental_text
    assert "扩容" in incremental_text
    assert "完整 smoke test" in incremental_text
    assert "--source-dir" in incremental_text and "--library-dir" in incremental_text
    assert "没有 plan 时" in incremental_text
    assert "不生成临时 change set" in incremental_text
    assert "无 plan 模式不写 plan" in incremental_text
    assert "按目标同步 `CURRENT` 标记" in incremental_text
    assert "跨池别名" in incremental_text
    assert "不得因原始 desc 非空而停止" in all_text
    assert "normal_missing_plot_count" in (ROOT / "scripts" / "scrape.py").read_text(encoding="utf-8")
    assert "audio_fingerprint.py" in optional_text
    assert "references/optional-tools.md" in skill_text
    assert "禁止只对源路径 `os.replace`" in incremental_text
    assert "shazamio" not in (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert (ROOT / "scripts" / "audio_fingerprint.py").is_file()

    table_header = "| Bangumi 独立 subject | AniDB 独立 aid | 库处理 |"
    assert skill_text.count(table_header) == 1
    for row in ("| 有 | 有 |", "| 有 | 无(", "| 无 | 有 |", "| 无 | 无 |"):
        assert skill_text.count(row) == 1, f"B+A 决策表行异常: {row}"
    print("  [ok] SKILL 文档契约（章节/引用/关键规则）正确")

def run() -> None:
    print("[contract] 文档/触发契约测试:")
    test_skill_document_contract()
    print("[contract] PASSED")


if __name__ == "__main__":
    run()
