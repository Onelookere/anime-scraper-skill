"""Offline filesystem, CLI and artwork integration checks."""
from __future__ import annotations

from smoke_support import *

def test_nfo_hardlink_update(td: Path):
    """重写源侧 NFO 时必须保留库侧共享 inode。"""
    video = _touch(td / "source" / "movie.mkv")
    source_nfo = video.with_suffix(".nfo")
    source_nfo.write_text("old", encoding="utf-8")
    library_nfo = td / "library" / "movie.nfo"
    library_nfo.parent.mkdir(parents=True)
    os.link(source_nfo, library_nfo)
    before = os.stat(source_nfo)

    show = match._show_from({"title": "更新后的电影", "plot": "new"})
    nfo.write_movie(show, video_path=video)
    source_stat, library_stat = os.stat(source_nfo), os.stat(library_nfo)
    assert (source_stat.st_dev, source_stat.st_ino) == (before.st_dev, before.st_ino)
    assert (source_stat.st_dev, source_stat.st_ino) == (
        library_stat.st_dev, library_stat.st_ino
    )
    assert source_nfo.read_bytes() == library_nfo.read_bytes()
    assert "更新后的电影" in library_nfo.read_text(encoding="utf-8")
    print("  [ok] 已硬链接 NFO 原位更新且源库 inode/内容一致")

def test_metadata_snapshot_cli(td: Path):
    """统一快照 CLI 只调用显式资源并写出稳定 JSON。"""
    from unittest.mock import patch

    calls = []

    def fake(name, payload):
        def invoke(identifier, cfg):
            calls.append((name, identifier, cfg["marker"]))
            return payload(identifier)
        return invoke

    output = td / "Hyouka_metadata.json"
    with config_root(td / "config"):
        cfg = load_test_config()
        cfg["marker"] = "test-config"
        (td / "config" / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8"
        )
        with (
            patch.object(metadata_snapshot.bangumi, "get_subject", fake("subject", lambda i: {"id": i})),
            patch.object(metadata_snapshot.bangumi, "get_episodes", fake("bgm_episodes", lambda i: [{"ep": i}])),
            patch.object(metadata_snapshot.bangumi, "get_characters", fake("characters", lambda i: [{"id": i}])),
            patch.object(metadata_snapshot.bangumi, "get_persons", fake("persons", lambda i: [{"id": i}])),
            patch.object(metadata_snapshot.bangumi, "get_theme_songs", fake("themes", lambda i: {"op": [i]})),
            patch.object(metadata_snapshot.anidb_episodes, "get_episodes", fake("anidb", lambda i: [{"aid": i}])),
            patch.object(metadata_snapshot.tmdb, "get_tv_detail", fake("tv_detail", lambda i: {"id": i})),
            patch.object(metadata_snapshot.tmdb, "get_season_episodes", lambda tv_id, season, cfg: [{"tv": tv_id, "season": season}]),
            patch.object(metadata_snapshot.tmdb, "get_tv_images", fake("tv_images", lambda i: {"id": i})),
            patch.object(metadata_snapshot.tmdb, "get_season_images", lambda tv_id, season, cfg: {"tv": tv_id, "season": season}),
        ):
            assert metadata_snapshot.main([
                "--output", str(output),
                "--bgm-id", "101",
                "--anidb-aid", "202",
                "--tmdb-tv-id", "303",
                "--tmdb-season", "303:0",
                "--tmdb-tv-images", "303",
                "--tmdb-season-images", "303:0",
            ]) == 0

    snapshot = json.loads(output.read_text(encoding="utf-8"))
    assert snapshot["schema"] == metadata_snapshot.SCHEMA
    assert snapshot["bangumi"]["subjects"]["101"]["subject"] == {"id": 101}
    assert snapshot["anidb"]["episodes"]["202"] == [{"aid": 202}]
    tv = snapshot["tmdb"]["tv"]["303"]
    assert tv["detail"] == {"id": 303}
    assert tv["seasons"]["0"]["episodes"] == [{"tv": 303, "season": 0}]
    assert tv["seasons"]["0"]["images"] == {"tv": 303, "season": 0}
    assert tv["images"] == {"id": 303}
    assert {name for name, _identifier, _marker in calls} == {
        "subject", "bgm_episodes", "characters", "persons", "themes",
        "anidb", "tv_detail", "tv_images",
    }
    assert all(marker == "test-config" for _name, _identifier, marker in calls)
    print("  [ok] 元数据快照 CLI 复用模块入口、缓存边界与显式资源范围")


def test_link_library(td: Path):
    """可选建库树:硬链接(同 inode)+ 字幕 + Season/Specials 结构 + 跳过 null。"""
    src = td / "src"
    v1 = _touch(src / "[G] Show [01][x265].mkv")
    _touch(src / "[G] Show [01][x265].JPSC.ass")            # 同名字幕
    _touch(src / "[G] Show [01][x265]-thumb.jpg")           # thumb 占位
    vop = _touch(src / "[G] Show [NCOP][x265].mkv")
    _touch(src / "[G] Show [NCOP][x265]-thumb.jpg")         # thumb 占位
    plan = {
        "type": "tv",
        "show": {"title": "Show", "premiered": "2023-06-29",
                 "anidb_aid": 1, "bgm_id": 2},
        "episodes": [
            {"category": "normal", "season": 1, "episode": 1, "title": "第一话",
             "video_path": str(v1)},
            {"category": "credit", "season": 0, "episode": 1, "title": "OP - 壱雫空",
             "anidb_epno": "C1", "video_path": str(vop)},
            {"category": "special", "season": 0, "episode": 2, "title": "跳过项",
             "video_path": None},
        ],
        "artwork": [
            {"scope": "episode", "kind": "episode_thumb",
             "source_path": str(src / "[G] Show [01][x265]-thumb.jpg"),
             "library_relpath": "Season 01/Show S01E01-thumb.jpg",
             "method": "frame", "fallback_video_path": str(v1)},
            {"scope": "episode", "kind": "episode_thumb",
             "source_path": str(src / "[G] Show [NCOP][x265]-thumb.jpg"),
             "library_relpath": "Specials/Show S00E01-thumb.jpg",
             "method": "frame", "fallback_video_path": str(vop)},
        ],
    }
    show, vps = match.assemble(plan)
    lib = td / "lib"
    written, skipped, n_vid, n_sub, show_dir = link_library.build_tv_tree(
        show, vps, str(lib), artwork=plan["artwork"])

    assert show_dir == lib / "Show (2023)", show_dir             # 扁平 Mixed 根 + 年份
    s1 = show_dir / "Season 01" / "Show S01E01.mkv"
    assert s1.exists() and images.verify_hardlink(v1, s1), "正片应为硬链接(同文件)"
    assert (show_dir / "Season 01" / "Show S01E01.nfo").exists()
    assert (show_dir / "Season 01" / "Show S01E01.JPSC.ass").exists(), "字幕应一并硬链"
    assert (show_dir / "Specials" / "Show S00E01.mkv").exists(), "OP 进 Specials"
    assert (show_dir / "tvshow.nfo").exists()
    assert n_vid == 2 and n_sub == 1, (n_vid, n_sub)
    assert skipped == ["跳过项"], skipped
    print("  [ok] link_library 扁平 Mixed 树(硬链接/字幕/季结构/跳过)正确")

def test_movie_tree(td: Path):
    """独立剧场版进入 Movies 子根,视频/字幕均为硬链接,NFO 使用电影命名。"""
    src = td / "src"
    vm = _touch(src / "[G] Movie [1080p].mkv")
    _touch(src / "[G] Movie [1080p]-thumb.jpg")  # thumb 占位
    sub = _touch(src / "[G] Movie [1080p].chs.ass")
    plan = {
        "type": "movie",
        "movie": {
            "title": "某剧场版", "plot": "电影简介", "premiered": "2023-03-24",
            "anidb_aid": 111, "bgm_id": 222, "video_path": str(vm),
        },
        "artwork": [
            {"scope": "movie", "kind": "episode_thumb",
             "source_path": str(src / "[G] Movie [1080p]-thumb.jpg"),
             "library_relpath": "某剧场版 (2023)-thumb.jpg",
             "method": "frame", "fallback_video_path": str(vm)},
        ],
    }
    show, extras_paths = match.assemble_movie(plan)
    lib = td / "lib"
    written, skipped, n_vid, n_sub, movie_dir = link_library.build_movie_tree(
        show, str(vm), str(lib), extras_paths=extras_paths, artwork=plan["artwork"])

    assert movie_dir == lib / "某剧场版 (2023)", movie_dir
    linked = movie_dir / "某剧场版 (2023).mkv"
    assert linked.exists() and images.verify_hardlink(vm, linked), "电影应为硬链接"
    assert (movie_dir / "某剧场版 (2023).nfo").exists()
    linked_sub = movie_dir / "某剧场版 (2023).chs.ass"
    assert linked_sub.exists() and images.verify_hardlink(sub, linked_sub), "电影字幕应一并硬链"
    assert len(written) == 1
    print("  [ok] 电影进入扁平 Mixed 根(硬链接/字幕/NFO)正确")

def test_multi_episode_ova_tree(td: Path):
    """高达UC式连续多集 OVA 使用 TV 模型:一季七集,而不是七部电影。"""
    src = td / "src"
    episodes = [_touch(src / f"OVA {i:02d}.mkv") for i in range(1, 8)]
    for i in range(1, 8):
        _touch(src / f"OVA {i:02d}-thumb.jpg")  # thumb 占位
    plan = {
        "type": "tv",
        "show": {"title": "七集OVA", "premiered": "2010-03-12", "anidb_aid": 3, "bgm_id": 4},
        "episodes": [
            {"category": "normal", "season": 1, "episode": i, "title": f"第{i}话",
             "video_path": str(path)}
            for i, path in enumerate(episodes, 1)
        ],
        "artwork": [
            {"scope": "episode", "kind": "episode_thumb",
             "source_path": str(src / f"OVA {i:02d}-thumb.jpg"),
             "library_relpath": f"Season 01/七集OVA S01E{i:02d}-thumb.jpg",
             "method": "frame", "fallback_video_path": str(src / f"OVA {i:02d}.mkv")}
            for i in range(1, 8)
        ],
    }
    show, paths = match.assemble(plan)
    lib = td / "lib"
    _, skipped, n_vid, _, show_dir = link_library.build_tv_tree(
        show, paths, str(lib), artwork=plan["artwork"])

    assert show_dir == lib / "七集OVA (2010)", show_dir
    assert n_vid == 7 and not skipped
    for i, src_video in enumerate(episodes, 1):
        linked = show_dir / "Season 01" / f"七集OVA S01E{i:02d}.mkv"
        assert linked.exists() and images.verify_hardlink(src_video, linked)
    assert not (lib / "七集OVA (2010)" / "extras").exists(), "连续 OVA 不应被建成电影 extras"
    print("  [ok] 连续七集 OVA 在扁平 Mixed 根按 TV 一季七集建树")

def test_scrape_hardlink_config_contract(td: Path):
    """配置开启即建库；显式 --no-hardlinks 才覆盖；dry-run 固化最终模式。"""
    src = td / "src"
    video = _touch(src / "[G] Contract [01].mkv")
    _touch(src / "[G] Contract [01]-thumb.jpg")  # thumb 占位
    library = td / "_Jellyfin"
    plan = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "tv", "output_dir": str(src),
        "library_projection": {
            "hardlinks_enabled": True, "link_root": str(library),
        },
        "show": {
            "title": "Contract",
            "sorttitle": "Contract 2024-01-01 Contract",
            "premiered": "2024-01-01",
            "lockdata": True,
        },
        "episodes": [{"category": "normal", "season": 1, "episode": 1,
                      "title": "第一话", "video_path": str(video),
                      "plot_evidence": {
                          "bangumi_zh": "empty", "tmdb_zh": "empty",
                          "bangumi_ja": "empty", "tmdb_en": "empty",
                      }}],
        "artwork": [
            {"scope": "episode", "kind": "episode_thumb",
             "source_path": str(src / "[G] Contract [01]-thumb.jpg"),
             "library_relpath": "Season 01/Contract S01E01-thumb.jpg",
             "method": "frame", "fallback_video_path": str(video)},
        ],
    }
    plan_file = td / "plan.json"
    plan_file.write_text(__import__("json").dumps(plan, ensure_ascii=False), encoding="utf-8")
    config_file = td / "config.json"
    config = load_test_config()
    config["paths"]["source_root"] = str(td)
    write_test_config(config_file, config)
    with config_root(td):
        assert scrape.main([
            "--plan", str(plan_file), "--dry-run",
            "--report-file", str(td / "dry-run-report.json"),
        ]) == 0
        assert scrape.main(["--plan", str(plan_file)]) == 0
    report = json.loads((td / "dry-run-report.json").read_text(encoding="utf-8"))
    projection = report["summary"]["library_projection"]
    assert projection["hardlinks_enabled"] is True
    assert projection["link_root"] == str(library)
    assert projection["decision_source"] == "config_enabled"
    assert report["summary"]["validations"]["library_projection"] == "passed"
    source_show = src / "tvshow.nfo"
    source_episode = video.with_suffix(".nfo")
    library_show = library / "Contract (2024)" / "tvshow.nfo"
    library_episode = library / "Contract (2024)" / "Season 01" / "Contract S01E01.nfo"
    assert source_show.exists() and source_episode.exists()
    assert source_show.read_bytes() == library_show.read_bytes()
    assert source_episode.read_bytes() == library_episode.read_bytes()
    assert images.verify_hardlink(source_show, library_show)
    assert images.verify_hardlink(source_episode, library_episode)
    source_only = json.loads(json.dumps(plan, ensure_ascii=False))
    source_only["library_projection"] = {
        "hardlinks_enabled": False, "link_root": None,
    }
    source_only_file = td / "source-only-plan.json"
    source_only_file.write_text(json.dumps(source_only, ensure_ascii=False), encoding="utf-8")
    config = load_test_config()
    config["library"]["hardlinks"]["root"] = str(td / "unused")
    write_test_config(config_file, config)
    with config_root(td):
        assert scrape.main([
            "--plan", str(source_only_file), "--dry-run", "--no-hardlinks",
            "--report-file", str(td / "source-only-report.json"),
        ]) == 0
        try:
            scrape.main(["--plan", str(source_only_file), "--dry-run"])
            raise AssertionError("plan 关闭快照不得与配置开启状态混用")
        except ValueError as exc:
            assert "硬链接开关" in str(exc), str(exc)
        try:
            scrape.main(["--plan", str(plan_file), "--dry-run"])
            raise AssertionError("plan 目标根不得与当前配置目标漂移")
        except ValueError as exc:
            assert "硬链接目标" in str(exc), str(exc)
    source_report = json.loads((td / "source-only-report.json").read_text(encoding="utf-8"))
    source_projection = source_report["summary"]["library_projection"]
    assert source_projection["hardlinks_enabled"] is False
    assert source_projection["decision_source"] == "explicit_user_opt_out"
    print("  [ok] config 默认建库、显式 opt-out、plan 快照与 dry-run 护栏正确")

def test_lockdata_guard(td: Path):
    """tv/movie 缺失或关闭 lockdata 时，在任何写入前拒绝。"""
    td.mkdir(parents=True, exist_ok=True)
    cases = [
        ("tv-missing", {
            "plan_schema": scrape.PLAN_SCHEMA,
            "type": "tv",
            "output_dir": str(td / "tv-missing"),
            "show": {
                "title": "缺失锁定",
                "sorttitle": "缺失锁定 2024-01-01 缺失锁定",
                "premiered": "2024-01-01",
            },
        }),
        ("tv-false", {
            "plan_schema": scrape.PLAN_SCHEMA,
            "type": "tv",
            "output_dir": str(td / "tv-false"),
            "show": {
                "title": "关闭锁定",
                "sorttitle": "关闭锁定 2024-01-01 关闭锁定",
                "premiered": "2024-01-01",
                "lockdata": False,
            },
        }),
        ("movie-false", {
            "plan_schema": scrape.PLAN_SCHEMA,
            "type": "movie",
            "output_dir": str(td / "movie-false"),
            "movie": {
                "title": "电影锁定",
                "sorttitle": "电影锁定 2024-01-01 电影锁定",
                "premiered": "2024-01-01",
                "lockdata": False,
            },
        }),
    ]
    for name, plan in cases:
        plan_path = td / f"{name}.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        try:
            scrape.main(["--plan", str(plan_path), "--dry-run", "--no-hardlinks"])
            raise AssertionError(f"{name} 缺少/关闭 lockdata 时必须被拒绝")
        except ValueError as exc:
            assert "lockdata" in str(exc), str(exc)
        assert not (Path(plan["output_dir"]) / "tvshow.nfo").exists()
        assert not (Path(plan["output_dir"]) / "movie.nfo").exists()
    assert scrape._validate_lockdata({
        "type": "tv", "show": {"lockdata": True}
    })["status"] == "passed"
    print("  [ok] tv/movie 缺失或关闭 lockdata 时在写入前拒绝")

def test_no_overwrite_existing_library(td: Path):
    """1.0 建库不得覆盖既有作品目录。"""
    src = td / "src"
    video = _touch(src / "a.mkv")
    show = match._show_from({"title": "已有作品", "premiered": "2020-01-01"})
    show.episodes.append(match.MergedEpisode("normal", 1, 1, "第一话"))
    library = td / "library"
    existing = library / "已有作品 (2020)"
    existing.mkdir(parents=True)
    marker = existing / "manual.txt"
    marker.write_text("keep", encoding="utf-8")
    artwork = [
        {"scope": "episode", "kind": "episode_thumb",
         "source_path": str(src / "a-thumb.jpg"),
         "library_relpath": "Season 01/已有作品 S01E01-thumb.jpg",
         "method": "frame", "fallback_video_path": str(video)},
    ]
    try:
        link_library.build_tv_tree(show, [str(video)], library, artwork=artwork)
        raise AssertionError("既有作品目录应阻断")
    except FileExistsError:
        pass
    assert marker.read_text(encoding="utf-8") == "keep"
    print("  [ok] 既有未知库目录会阻断且不被覆盖")

def test_scan_tree(td: Path):
    """scan_tree 摆【结构素材】:目录树 + 集号分布 + 逐文件 hint(+可选时长)。

    验证 identify 已退为"素材提供者":切单元/归类不在脚本里做,这里只保证素材齐整、
    hint 归到子对象、flat 多季(根目录与子文件夹都从 ep1 起)在集号分布里看得见。
    """
    # (A) 规范多文件夹单元 + SPs 子目录特殊集
    _touch(td / "SHOW A" / "[G] Show A [01][x265].mkv")
    _touch(td / "SHOW A" / "[G] Show A [02][x265].mkv")
    _touch(td / "SHOW A" / "SPs" / "[G] Show A [NCOP][x265].mkv")
    # (B) 根目录 flat 平铺(集号又从 1 起 = 多季/多单元重置)
    _touch(td / "[G] Flat [01].mkv")
    _touch(td / "[G] Flat [02].mkv")

    data = identify.scan_tree(td)          # 不传 probe_fn → duration 全 None(无 ffmpeg 依赖)

    assert data["video_count"] == 5, data["video_count"]
    assert "SHOW A" in data["tree_text"], data["tree_text"]
    # 每个文件:硬事实在顶层 + 正则初判归到 hint 子对象
    for rec in data["files"]:
        assert rec["rel_path"] and rec["stem"], rec
        assert rec["duration"] is None, "未传 probe_fn 时时长应为 None"
        assert "episode_number" in rec["hint"] and "episode_type" in rec["hint"], rec

    # SPs 里的 NCOP:子目录提示 + hint 初判为 credit(agent 复核用)
    sp = next(r for r in data["files"] if "NCOP" in r["stem"])
    assert sp["subdir_special_hint"] is True, sp
    assert sp["hint"]["episode_type"] == "credit", sp

    # 集号分布:根目录与子文件夹各自从 ep1 起 → flat 重置在此可见
    by_dir = data["by_dir"]
    assert sorted(by_dir.get("", [])) == [1, 2], by_dir
    show_a = next((k for k in by_dir if "SHOW A" in k), None)
    assert show_a is not None and sorted(by_dir[show_a]) == [1, 2], by_dir

    # 注入 probe_fn → 时长被填(阶段一一站式盘点用)
    data2 = identify.scan_tree(td, probe_fn=lambda p: 84.0)
    assert all(r["duration"] == 84.0 for r in data2["files"]), "probe_fn 应注入时长"
    print("  [ok] scan_tree 结构素材(目录树/集号分布/hint/时长注入)正确")

def test_scan_cli_manifest_summary(td: Path):
    """CLI 默认只打印有界摘要，完整目录树/逐文件证据写入 manifest。"""
    _touch(td / "Show" / "[G] Show [01].mkv")
    _touch(td / "Show" / "SPs" / "[G] Show [NCOP].mkv")
    manifest = td / "scan-manifest.json"
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        assert identify.main([str(td), "--manifest", str(manifest)]) == 0

    rendered = stdout.getvalue()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["schema"] == "anime-scraper-scan-manifest-v1"
    assert payload["scan"]["video_count"] == 2
    assert all(item["duration"] is None for item in payload["scan"]["files"])
    assert payload["summary"]["special_hint_count"] == 1
    assert "完整 manifest:" in rendered
    assert "tree_text" not in rendered and "series_hint=" not in rendered
    assert "[G] Show [NCOP].mkv" not in rendered, "完整逐文件路径不应默认刷屏"
    print("  [ok] identify CLI 完整 manifest 留盘且终端仅输出摘要")

def test_scrape_dry_run_report_summary(td: Path):
    """dry-run 完整 plan/report 留盘，stdout 不再倾倒大 JSON。"""
    video = _touch(td / "src" / "episode01.mkv")
    thumb = _touch(td / "src" / "episode01-thumb.jpg")
    marker = "PLOT-MUST-STAY-IN-REPORT-ONLY"
    original_cache_dir = td / "cache" / "报告测试-20240101T000000Z"
    plan = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "tv",
        "output_dir": str(td / "src"),
        "library_projection": {"hardlinks_enabled": False, "link_root": None},
        "show": {
            "title": "报告测试",
            "sorttitle": "报告测试 2024-01-01 报告测试",
            "plot": "作品简介",
            "premiered": "2024-01-01",
            "bgm_id": 1,
            "anidb_aid": 2,
            "staff_status": "empty",
            "staff_audit": {"persons_checked": True, "mappable_crew_count": 0},
            "lockdata": True,
        },
        "episodes": [{
            "category": "normal", "season": 1, "episode": 1,
            "title": "第一话", "plot": marker, "airdate": "2024-01-01",
            "video_path": str(video),
        }],
        "artwork_review": {"original_cache_dir": str(original_cache_dir)},
        "artwork": [{
            "scope": "episode", "kind": "episode_thumb",
            "source_path": str(thumb), "method": "frame",
            "fallback_video_path": str(video),
        }],
    }
    plan_file = td / "plan.json"
    report_file = td / "dry-run-report.json"
    plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        assert scrape.main([
            "--plan", str(plan_file), "--dry-run", "--no-hardlinks",
            "--report-file", str(report_file),
        ]) == 0

    rendered = stdout.getvalue()
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["schema"] == "anime-scraper-dry-run-report-v1"
    assert report["source_plan"] == plan
    assert report["summary"]["normal_episode_count"] == 1
    assert report["summary"]["normal_missing_plot_count"] == 0
    assert report["summary"]["special_missing_plot_count"] == 0
    assert report["summary"]["validations"]["episode_plots"] == "passed"
    assert report["summary"]["validations"]["special_airdates"] == "passed"
    assert report["summary"]["validations"]["source_media"] == "passed"
    assert report["summary"]["original_cache_dir"] == str(original_cache_dir)
    assert marker in report_file.read_text(encoding="utf-8")
    assert marker not in rendered and '"source_plan"' not in rendered
    assert f"原图缓存目录: {original_cache_dir}" in rendered
    assert "完整 dry-run 报告:" in rendered
    assert not video.with_suffix(".nfo").exists(), "dry-run 不得写媒体侧 NFO"

    # dry-run 成功后进度账本记录 dry_run 阶段;账本由脚本维护、schema 固定。
    ledger_path = scrape.progress_ledger_path(plan_file)
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["schema"] == scrape.PROGRESS_SCHEMA
    assert ledger["title"] == "报告测试" and ledger["type"] == "tv"
    assert ledger["stages"]["dry_run"]["report_file"] == str(report_file)
    assert "source" not in ledger["stages"] and "library" not in ledger["stages"]
    print("  [ok] scrape dry-run 完整报告留盘且终端仅输出审计摘要")

def test_show_staff_guard():
    """有 Bangumi 身份时，作品级 staff 必须显式 present/empty 且自洽。"""
    base = {"type": "tv", "show": {"bgm_id": 1, "actors": [
        {"name": "声优", "type": "Actor"},
    ]}}
    try:
        scrape._validate_show_staff(base)
        raise AssertionError("缺少 staff_status 必须被拒绝")
    except ValueError as exc:
        assert "staff_status" in str(exc), str(exc)

    present = json.loads(json.dumps(base, ensure_ascii=False))
    present["show"]["staff_status"] = "present"
    present["show"]["actors"].append({
        "name": "导演", "role": "导演", "type": "Director",
    })
    assert scrape._validate_show_staff(present)["status"] == "passed"

    duplicated_note = json.loads(json.dumps(present, ensure_ascii=False))
    duplicated_note["show"]["staff_note"] = "导演：另一位导演"
    try:
        scrape._validate_show_staff(duplicated_note)
        raise AssertionError("staff_note 重复可映射职位必须被拒绝")
    except ValueError as exc:
        assert "staff_note 不得包含可映射职位" in str(exc), str(exc)

    unmappable_note = json.loads(json.dumps(present, ensure_ascii=False))
    unmappable_note["show"]["staff_note"] = "美术监督：美术监督甲"
    assert scrape._validate_show_staff(unmappable_note)["status"] == "passed"

    note_only = json.loads(json.dumps(base, ensure_ascii=False))
    note_only["show"].update({
        "staff_status": "present", "staff_note": "人物设定:甲",
    })
    try:
        scrape._validate_show_staff(note_only)
        raise AssertionError("没有 staff_audit 的 note-only staff 必须被拒绝")
    except ValueError as exc:
        assert "staff_audit" in str(exc), str(exc)

    stale = json.loads(json.dumps(note_only, ensure_ascii=False))
    stale["show"]["staff_audit"] = {
        "persons_checked": True, "mappable_crew_count": 1,
    }
    try:
        scrape._validate_show_staff(stale)
        raise AssertionError("审计显示有 crew 但 actors 为空必须被拒绝")
    except ValueError as exc:
        assert "可映射 crew" in str(exc), str(exc)

    audited_empty = json.loads(json.dumps(note_only, ensure_ascii=False))
    audited_empty["show"]["staff_audit"] = {
        "persons_checked": True, "mappable_crew_count": 0,
    }
    assert scrape._validate_show_staff(audited_empty)["status"] == "passed"

    empty = json.loads(json.dumps(base, ensure_ascii=False))
    empty["show"]["staff_status"] = "empty"
    empty["show"]["staff_audit"] = {
        "persons_checked": True, "mappable_crew_count": 0,
    }
    assert scrape._validate_show_staff(empty)["status"] == "passed"

    inconsistent = json.loads(json.dumps(present, ensure_ascii=False))
    inconsistent["show"]["staff_status"] = "empty"
    try:
        scrape._validate_show_staff(inconsistent)
        raise AssertionError("staff_status 与内容矛盾必须被拒绝")
    except ValueError as exc:
        assert "矛盾" in str(exc), str(exc)
    print("  [ok] 作品级 staff 必须显式审查且状态与内容一致")

def test_episode_plot_guards(td: Path):
    """staff 行必须阻断；剧情通过；正片空简介单独告警。"""
    media = _touch(td / "episode01.mkv")
    base = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "tv",
        "show": {"title": "简介护栏", "sorttitle": "简介护栏 9999-12-31 简介护栏"},
        "episodes": [{"category": "normal", "season": 1, "episode": 1,
                      "title": "第一话", "video_path": str(media)}],
    }
    bad = json.loads(json.dumps(base, ensure_ascii=False))
    bad["episodes"][0]["plot"] = "脚本：花田十辉\n分镜：测试"
    try:
        scrape._validate_episode_plots(bad)
        raise AssertionError("staff-only plot 必须被拒绝")
    except ValueError as exc:
        assert "plot 含 staff 字段" in str(exc), str(exc)

    clean = json.loads(json.dumps(base, ensure_ascii=False))
    clean["episodes"][0]["plot"] = "冈部发现世界线发生了变化。"
    assert scrape._validate_episode_plots(clean)["status"] == "passed"

    try:
        scrape._validate_episode_plots(base)
        raise AssertionError("正片空 plot 缺少来源证据必须被拒绝")
    except ValueError as exc:
        assert "plot_evidence" in str(exc), str(exc)
    defaulted = json.loads(json.dumps(base, ensure_ascii=False))
    del defaulted["episodes"][0]["season"]
    try:
        scrape._validate_episode_plots(defaulted)
        raise AssertionError("category=normal 的默认 season 也必须进入正片校验")
    except ValueError as exc:
        assert "plot_evidence" in str(exc), str(exc)

    exhausted = json.loads(json.dumps(base, ensure_ascii=False))
    exhausted["episodes"][0]["plot_evidence"] = {
        "bangumi_zh": "empty", "tmdb_zh": "empty",
        "bangumi_ja": "empty", "tmdb_en": "empty",
    }
    assert scrape._validate_episode_plots(exhausted)["status"] == "passed"
    present = json.loads(json.dumps(exhausted, ensure_ascii=False))
    present["episodes"][0]["plot_evidence"]["tmdb_zh"] = "present"
    try:
        scrape._validate_episode_plots(present)
        raise AssertionError("仍有来源简介时的空 plot 必须被拒绝")
    except ValueError as exc:
        assert "present" in str(exc), str(exc)

    show, paths = match.assemble(exhausted)
    summary = scrape._dry_run_summary(
        show, paths, [], plan=exhausted, plan_type="tv", link_enabled=False,
    )
    assert summary["normal_missing_plot_count"] == 1
    assert summary["special_missing_plot_count"] == 0
    assert summary["validations"]["episode_plots"] == "passed"
    assert summary["plot_validation"]["exhausted_labels"] == ["第一话"]
    assert any("四级来源均明确为空" in warning for warning in summary["warnings"])
    print("  [ok] 分集 plot 拒绝 staff/未穷尽来源，四级均空时才放行")

def test_show_plot_guards(td: Path):
    """作品级 plot 只能含剧情；staff_note 不得被重复写入简介。"""
    base = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "tv",
        "show": {
            "title": "作品简介护栏",
            "plot": "这是剧情简介。",
            "sorttitle": "作品简介护栏 9999-12-31 作品简介护栏",
        },
    }
    assert scrape._validate_show_plot(base)["status"] == "passed"

    marked = json.loads(json.dumps(base, ensure_ascii=False))
    marked["show"]["plot"] = "这是剧情简介。\n\n【制作】人物设定:甲"
    marked["show"]["staff_note"] = "人物设定:甲"
    try:
        scrape._validate_show_plot(marked)
        raise AssertionError("作品级 plot 含制作行必须被拒绝")
    except ValueError as exc:
        assert "作品级 plot" in str(exc), str(exc)

    overlap = json.loads(json.dumps(base, ensure_ascii=False))
    overlap["show"]["plot"] = "这是剧情简介。\n\n无标准职位:乙"
    overlap["show"]["staff_note"] = "无标准职位:乙"
    try:
        scrape._validate_show_plot(overlap)
        raise AssertionError("作品级 plot 与 staff_note 重叠必须被拒绝")
    except ValueError as exc:
        assert "staff_note 重叠" in str(exc), str(exc)
    print("  [ok] 作品级 plot 拒绝 staff 行和 staff_note 重叠")

def test_special_order_guard(td: Path):
    """Season 0 必须使用 Agent 排序结果，不能静默沿用交错的源文件顺序。"""
    media = [_touch(td / f"special{i}.mkv") for i in range(1, 4)]
    plan = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "tv",
        "episodes": [
            {"category": "special", "season": 0, "episode": 1,
             "title": "OVA 1", "video_path": str(media[0]),
             "special_order": {"priority": 10, "series_key": "OVA",
                                "series_order": 1, "item_order": 1, "source_index": 0}},
            {"category": "credit", "season": 0, "episode": 2,
             "title": "OP", "video_path": str(media[1]),
             "special_order": {"priority": 20, "series_key": "OP",
                                "series_order": 1, "item_order": 1, "source_index": 1}},
            {"category": "other", "season": 0, "episode": 3,
             "title": "录音花絮", "video_path": str(media[2]),
             "special_order": {"priority": 40, "series_key": "录音花絮",
                                "series_order": 1, "item_order": 1, "source_index": 2}},
        ],
    }
    result = scrape._validate_special_order(plan)
    assert result["status"] == "passed" and result["series_count"] == 3, result

    bad = json.loads(json.dumps(plan, ensure_ascii=False))
    bad["episodes"][1]["special_order"]["priority"] = 40
    bad["episodes"][2]["special_order"]["priority"] = 20
    try:
        scrape._validate_special_order(bad)
        raise AssertionError("交错 Season 0 顺序必须被拒绝")
    except ValueError as exc:
        assert "未按 Agent 给出的 special_order 排列" in str(exc), str(exc)

    missing = json.loads(json.dumps(plan, ensure_ascii=False))
    del missing["episodes"][1]["special_order"]
    try:
        scrape._validate_special_order(missing)
        raise AssertionError("缺少 special_order 必须被拒绝")
    except ValueError as exc:
        assert "缺少 special_order" in str(exc), str(exc)
    print("  [ok] Season 0 排序必须由 Agent 给出且乱序/缺字段会被拒绝")


def test_special_titles_guard(td: Path):
    """Season 0 标题护栏：空标题/技术标签拒绝；特典 N 且文件名有标题证据时告警。"""
    live = td / "Live [AI-Raws] 凱旋公演 第一壁 (BD HEVC 1920x1080 FLAC)[AB12CD34].mkv"
    _touch(live)
    plain = td / "[Raws] SP01 (BD HEVC 1920x1080 FLAC)[AB12CD34].mkv"
    _touch(plain)

    def make_plan(title, path):
        return {
            "plan_schema": scrape.PLAN_SCHEMA,
            "type": "tv",
            "show": {"title": "某作品"},
            "episodes": [
                {"category": "special", "season": 0, "episode": 1,
                 "title": title, "video_path": str(path)},
            ],
        }

    ok = scrape._validate_special_titles(make_plan("总员集结 凯旋公演 第一壁", live))
    assert ok["status"] == "passed" and not ok["warnings"], ok

    fallback_named = scrape._validate_special_titles(make_plan("特典 1", plain))
    assert fallback_named["status"] == "passed" and not fallback_named["warnings"], fallback_named

    fallback_titled = scrape._validate_special_titles(make_plan("特典 1", live))
    assert fallback_titled["status"] == "passed", fallback_titled
    assert any("疑似携带明确标题" in w for w in fallback_titled["warnings"]), fallback_titled

    for bad_title in ("", "   ", "凱旋公演 第一壁 (BD HEVC 1920x1080 FLAC)[AB12CD34]"):
        try:
            scrape._validate_special_titles(make_plan(bad_title, live))
            raise AssertionError(f"坏标题必须被拒绝: {bad_title!r}")
        except ValueError as exc:
            assert "Season 0 标题护栏失败" in str(exc), str(exc)

    # 裸 BD/DVD 是载体词不是压制标签:special-rules.md §4.3.7 要求优先采用
    # 文件名里的真实标题,而这类标题常含 BD/DVD。误判会直接中止整次刮削。
    for real_title in ("BD Box 購入特典 スペシャルドラマ", "DVD Vol.1 映像特典",
                       "劇場版 BD 特典ディスク", "ライブ映像「Blu-ray Disc BOX」"):
        passed = scrape._validate_special_titles(make_plan(real_title, live))
        assert passed["status"] == "passed", (real_title, passed)
    # 但文件名清洗仍要剥掉载体词,才能判断残余是否还有真实标题。
    assert "BD" not in scrape._special_title_stem_remnant(
        "Show BD 凱旋公演.mkv", "Show")
    print("  [ok] Season 0 标题护栏拦截空标题/技术标签并提示特典 N 兜底证据")

def test_empty_primary_plot_dry_run_report(td: Path):
    """空正片简介先写失败报告，再阻止任何 NFO 落盘。"""
    video = _touch(td / "episode01.mkv")
    thumb = _touch(td / "episode01-thumb.jpg")
    plan = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "tv",
        "output_dir": str(td),
        "library_projection": {"hardlinks_enabled": False, "link_root": None},
        "show": {"title": "空简介报告", "sorttitle": "空简介报告 2024-01-01 空简介报告",
                 "premiered": "2024-01-01", "lockdata": True},
        "episodes": [{"category": "normal", "season": 1, "episode": 1,
                      "title": "第一话", "plot": "", "video_path": str(video)}],
        "artwork": [{
            "scope": "episode", "kind": "episode_thumb",
            "source_path": str(thumb), "method": "frame",
            "fallback_video_path": str(video),
        }],
    }
    plan_file = td / "empty-plot-plan.json"
    report_file = td / "empty-plot-report.json"
    plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    with config_root(td), contextlib.redirect_stdout(io.StringIO()):
        try:
            scrape.main([
                "--plan", str(plan_file), "--dry-run", "--no-hardlinks",
                "--report-file", str(report_file),
            ])
            raise AssertionError("未提供 plot_evidence 的正片空 plot 必须拒绝")
        except ValueError as exc:
            assert "plot 校验失败" in str(exc), str(exc)
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["summary"]["validations"]["episode_plots"] == "failed"
    assert report["summary"]["plot_validation"]["empty_labels"] == ["第一话"]
    assert not video.with_suffix(".nfo").exists()
    print("  [ok] 空正片 plot 的 dry-run 先留失败报告再阻止落盘")

def test_source_only_preflight_guards(td: Path):
    """source-only 必须在写 NFO 前拒绝缺媒体、缺 thumb 和缺方法字段。"""
    src = td / "src"
    video = _touch(src / "episode01.mkv")
    expected_thumb = src / "episode01-thumb.jpg"

    def rejected(plan: dict, name: str, expected: str, *, dry_run: bool = True):
        plan_file = td / f"{name}.json"
        plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        native_args = ["--plan", str(plan_file)]
        if dry_run:
            native_args.append("--dry-run")
        try:
            scrape.main([*native_args, "--no-hardlinks"])
            raise AssertionError(f"{name} 应被 source-only preflight 拒绝")
        except (ValueError, FileNotFoundError) as exc:
            assert expected in str(exc), str(exc)

    base = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "tv", "output_dir": str(src),
        "library_projection": {"hardlinks_enabled": False, "link_root": None},
        "show": {"title": "源侧护栏", "sorttitle": "源侧护栏 2024-01-01 源侧护栏",
                 "premiered": "2024-01-01", "lockdata": True},
        "episodes": [{"category": "normal", "season": 1, "episode": 1,
                      "title": "第一话", "video_path": str(video),
                      "plot_evidence": {
                          "bangumi_zh": "empty", "tmdb_zh": "empty",
                          "bangumi_ja": "empty", "tmdb_en": "empty",
                      }}],
        "artwork": [],
    }
    bad_output = json.loads(json.dumps(base, ensure_ascii=False))
    bad_output["output_dir"] = str(td / "meta")
    rejected(bad_output, "forbidden-meta-output", "末级目录不得恰为 meta")

    rejected(base, "missing-thumb", "源侧同 stem thumb", dry_run=False)
    assert not video.with_suffix(".nfo").exists(), "preflight 失败前不得写 NFO"

    missing_video = td / "missing" / "episode01.mkv"
    bad_media = json.loads(json.dumps(base, ensure_ascii=False))
    bad_media["episodes"][0]["video_path"] = str(missing_video)
    bad_media["artwork"] = [{
        "scope": "episode", "kind": "episode_thumb",
        "source_path": str(missing_video.with_name("episode01-thumb.jpg")),
        "method": "tmdb", "url": "https://example.invalid/not-used",
    }]
    rejected(bad_media, "missing-video", "媒体路径不存在")

    missing_url = json.loads(json.dumps(base, ensure_ascii=False))
    missing_url["artwork"] = [{
        "scope": "episode", "kind": "episode_thumb",
        "source_path": str(expected_thumb), "method": "tmdb",
    }]
    rejected(missing_url, "missing-url", "必须提供 url")

    still_thumb = _touch(expected_thumb)
    still_plan = json.loads(json.dumps(base, ensure_ascii=False))
    still_plan["episodes"][0]["tmdb_still_url"] = "https://image.tmdb.org/t/p/original/still.jpg"
    still_plan["artwork"] = [{
        "scope": "episode", "kind": "episode_thumb",
        "source_path": str(still_thumb), "method": "frame",
        "fallback_video_path": str(video),
    }]
    rejected(still_plan, "tmdb-still-frame", "TMDB still 优先护栏失败")
    still_plan["artwork"][0] = {
        "scope": "episode", "kind": "episode_thumb",
        "source_path": str(still_thumb), "method": "tmdb",
        "url": still_plan["episodes"][0]["tmdb_still_url"],
    }
    still_plan_path = td / "tmdb-still-pass.json"
    still_plan_path.write_text(json.dumps(still_plan, ensure_ascii=False), encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        assert scrape.main([
            "--plan", str(still_plan_path), "--dry-run", "--no-hardlinks"
        ]) == 0

    movie_plan = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "movie", "output_dir": str(src),
        "library_projection": {"hardlinks_enabled": False, "link_root": None},
        "movie": {"title": "电影源侧护栏", "sorttitle": "电影源侧护栏 2024-01-01 电影源侧护栏",
                  "premiered": "2024-01-01", "lockdata": True,
                  "video_path": str(video)},
        "artwork": [],
    }
    rejected(movie_plan, "movie-missing-thumb", "源侧同 stem thumb")
    print("  [ok] source-only preflight 在写入前拒绝缺媒体、缺 thumb 与缺方法字段")

def test_tmdb_special_identity_guard(td: Path):
    """Season 0 的远程 still 必须有唯一且年份一致的 TMDB 身份。"""
    video = _touch(td / "special.mkv")
    base = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "tv",
        "show": {"title": "续作", "premiered": "2004-11-26"},
        "episodes": [{
            "season": 0, "episode": 1, "title": "科学讲座",
            "video_path": str(video), "tmdb_match_status": "matched",
            "tmdb_still_url": "https://image.tmdb.org/t/p/original/still.jpg",
        }],
    }
    for plan, expected in (
        (base, "tmdb_identity"),
        ({**base, "tmdb_identity": {
            "id": 66931, "name": "合并条目", "original_name": "Merged",
            "first_air_date": "1988-10-07", "status": "ambiguous",
        }}, "作品身份护栏"),
    ):
        try:
            scrape._validate_tmdb_special_identity(plan, plan_type="tv")
            raise AssertionError("不安全的 Season 0 TMDB 身份必须被拒绝")
        except ValueError as exc:
            assert expected in str(exc), str(exc)

    verified = json.loads(json.dumps(base, ensure_ascii=False))
    verified["tmdb_identity"] = {
        "id": 2004, "name": "续作", "original_name": "Sequel",
        "first_air_date": "2004-01-01", "status": "verified",
    }
    assert scrape._validate_tmdb_special_identity(
        verified, plan_type="tv"
    )["status"] == "passed"

    frame_only = json.loads(json.dumps(base, ensure_ascii=False))
    frame_only["episodes"][0].pop("tmdb_still_url")
    frame_only["episodes"][0]["tmdb_match_status"] = "unknown"
    assert scrape._validate_tmdb_special_identity(
        frame_only, plan_type="tv"
    )["status"] == "not_required"

    fallback = json.loads(json.dumps(frame_only, ensure_ascii=False))
    fallback["tmdb_identity"] = {
        "id": 66931, "name": "合并条目", "original_name": "Merged",
        "first_air_date": "1988-10-07", "status": "ambiguous",
        "reason": "TMDB 父条目跨作合并",
    }
    fallback_result = scrape._validate_tmdb_special_identity(
        fallback, plan_type="tv"
    )
    assert fallback_result["status"] == "fallback"
    assert fallback_result["selection"] == "frame"
    print("  [ok] Season 0 TMDB 身份快照、跨作合并与年份不一致护栏正确")

def test_artwork_visual_review(td: Path):
    """320x480 候选不拉伸；五候选上限、分 sheet 与 plan 选择护栏可离线验证。"""
    td.mkdir(parents=True, exist_ok=True)

    def candidate(group: int, index: int) -> dict:
        return _image_candidate(
            f"/g{group}-c{index}.jpg",
            language="ja" if index % 2 else "zh",
            votes=10 - index,
            width=1000,
            height=1400,
        )

    request_payload = {
        "series_name": "视觉测试",
        "groups": [
            {
                "group_id": f"season-{group}",
                "label": f"Season {group}",
                "work_name": f"Season {group}",
                "candidates": [candidate(group, index) for index in range(1, 7)],
                "cache_candidates": [candidate(group, index) for index in range(1, 7)],
            }
            for group in range(1, 5)
        ]
    }

    load_calls = 0

    def loader(item: dict) -> bytes:
        nonlocal load_calls
        load_calls += 1
        index = int(re.search(r"c(\d+)", item["file_path"]).group(1))
        source = artwork_review.Image.new(
            "RGB", (100, 140), (180, 30 + index * 10, 40)
        )
        payload = io.BytesIO()
        source.save(payload, "PNG")
        return payload.getvalue()

    review = artwork_review.build_review(
        request_payload, td / "review", loader=loader, multimodal_enabled=True,
        preview_cache_dir=td / "preview-cache",
    )
    assert review["status"] == "pending_agent_review"
    assert review["multimodal_review_enabled"] is True
    assert review["selection_method"] == "agent_multimodal"
    assert review["preview"]["width"] == 320
    assert review["preview"]["height"] == 480
    assert review["candidate_limit"] == 5
    assert review["preview_cache"] == {"enabled": True, "hits": 0, "misses": 20}
    first_load_calls = load_calls
    cached_review = artwork_review.build_review(
        request_payload, td / "review-cached", loader=loader,
        multimodal_enabled=True, preview_cache_dir=td / "preview-cache",
    )
    assert load_calls == first_load_calls, "缓存命中时不得重复下载候选预览"
    assert cached_review["preview_cache"] == {
        "enabled": True, "hits": 20, "misses": 0,
    }
    assert all(len(group["candidates"]) == 5 for group in review["groups"])
    assert len(review["sheets"]) == 2
    row_height = artwork_review.PREVIEW_SIZE[1] + artwork_review.LABEL_HEIGHT
    assert (review["sheets"][0]["width"], review["sheets"][0]["height"]) == (1600, row_height * 3)
    assert (review["sheets"][1]["width"], review["sheets"][1]["height"]) == (1600, row_height)
    first_candidate = review["groups"][0]["candidates"][0]
    assert first_candidate["resolution_class"] == "acceptable"
    assert "1000x1400" in " ".join(artwork_review._candidate_label(first_candidate))

    preview_path = Path(review["groups"][0]["candidates"][0]["preview_path"])
    with artwork_review.Image.open(preview_path) as preview:
        assert preview.size == (320, 480)
        top = preview.getpixel((160, 5))
        content = preview.getpixel((160, 30))
        assert max(abs(top[i] - artwork_review.BACKGROUND[i]) for i in range(3)) < 8
        assert content[0] > 140 and content[1] < 100, (top, content)

    review["status"] = "completed"
    for group in review["groups"]:
        for index, candidate_item in enumerate(group["candidates"]):
            candidate_item["visible_text_role"] = (
                "primary_title" if index == 0 else "none"
            )
            candidate_item["primary_title_prominence"] = 3 if index == 0 else 0
            candidate_item["visual_issues"] = []
    review["selections"] = [
        {
            "group_id": group["group_id"],
            "candidate_id": group["candidates"][0]["candidate_id"],
            "confidence": "high",
            "reason": "构图完整、无明显水印，跨季度风格协调",
            "flags": [],
            "decision_factors": {
                "language": "中文或日文标题优先，当前候选语言合适",
                "resolution": "原始尺寸达到 acceptable",
                "title": "作品主标题完整且醒目",
                "visual_quality": "无水印、损坏或严重裁切",
            },
        }
        for group in review["groups"]
    ]
    plan_review = artwork_review.compact_plan_review(review)
    assert "sheets" not in plan_review
    assert "preview_path" not in plan_review["groups"][0]["candidates"][0]
    assert plan_review["groups"][0]["candidates"][0]["width"] == 1000
    assert plan_review["groups"][0]["candidates"][0]["resolution_class"] == "acceptable"
    assert plan_review["groups"][0]["candidates"][0]["language"] == "ja"
    assert re.fullmatch(r"[0-9a-f]{64}", plan_review["source_manifest_sha256"])
    completed_manifest_path = td / "completed-artwork-review.json"
    compact_path = td / "artwork-review-plan.json"
    completed_manifest_path.write_text(
        json.dumps(review, ensure_ascii=False), encoding="utf-8"
    )
    with contextlib.redirect_stdout(io.StringIO()):
        assert artwork_review.main([
            "--compact-review", str(completed_manifest_path),
            "--plan-review-out", str(compact_path),
        ]) == 0
    assert json.loads(compact_path.read_text(encoding="utf-8")) == plan_review
    selected = review["groups"][0]["candidates"][0]
    plan = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "artwork_review": plan_review,
        "artwork": [{
            "scope": "season", "kind": "poster", "method": "tmdb",
            "url": selected["url"], "source_path": str(td / "poster.jpg"),
        }],
    }
    validation = scrape._validate_artwork_review(
        plan, plan["artwork"], multimodal_enabled=True
    )
    assert validation["status"] == "passed"
    alternative = review["groups"][0]["candidates"][1]
    special_plan = json.loads(json.dumps(plan))
    special_plan["artwork"].append({
        "scope": "season", "kind": "poster", "method": "tmdb",
        "url": alternative["url"], "source_path": str(td / "specials-poster.jpg"),
        "library_relpath": "Specials/poster.jpg",
        "specials_selection": "main_pool_alternative",
    })
    assert scrape._validate_artwork_review(
        special_plan, special_plan["artwork"], multimodal_enabled=True
    )["status"] == "passed"
    special_plan["artwork"][-1]["specials_selection"] = "main_reuse"
    try:
        scrape._validate_artwork_review(
            special_plan, special_plan["artwork"], multimodal_enabled=True
        )
        raise AssertionError("Specials 海报不能以废弃 main_reuse 绕过审查")
    except ValueError as exc:
        assert "未命中 Agent 识图选择" in str(exc)
    incomplete_judgment = json.loads(json.dumps(plan))
    incomplete_judgment["artwork_review"]["selections"][0].pop("decision_factors")
    try:
        scrape._validate_artwork_review(
            incomplete_judgment, incomplete_judgment["artwork"], multimodal_enabled=True
        )
        raise AssertionError("综合选图不得缺少四项 decision_factors")
    except ValueError as exc:
        assert "四项综合判断" in str(exc)
    holistic_choice = json.loads(json.dumps(plan))
    resolution_group = holistic_choice["artwork_review"]["groups"][0]
    low_candidate = resolution_group["candidates"][0]
    better_candidate = resolution_group["candidates"][1]
    low_candidate.update({
        "width": 562, "height": 750, "resolution_class": "low",
        "visible_text_role": "primary_title", "primary_title_prominence": 3,
    })
    better_candidate.update({
        "width": 2000, "height": 3000, "resolution_class": "preferred",
        "visible_text_role": "primary_title", "primary_title_prominence": 2,
    })
    assert scrape._validate_artwork_review(
        holistic_choice, holistic_choice["artwork"],
        multimodal_enabled=True,
    )["status"] == "passed", "综合判断不得由分辨率单项机械否决"
    fatal_choice = json.loads(json.dumps(plan))
    fatal_choice["artwork_review"]["groups"][0]["candidates"][0]["visual_issues"] = [
        "third_party_watermark"
    ]
    try:
        scrape._validate_artwork_review(
            fatal_choice, fatal_choice["artwork"], multimodal_enabled=True
        )
        raise AssertionError("第三方水印候选不得入选")
    except ValueError as exc:
        assert "不可接受的视觉缺陷" in str(exc)
    wrong_schema_plan = json.loads(json.dumps(plan))
    wrong_schema_plan["plan_schema"] = "invalid-plan-schema"
    try:
        scrape._require_plan_schema(wrong_schema_plan)
        raise AssertionError("非当前契约的 plan_schema 应被拒绝")
    except ValueError as exc:
        assert "plan_schema 必须为" in str(exc), str(exc)

    video = _touch(td / "source" / "episode01.mkv")
    thumb = _touch(td / "source" / "episode01-thumb.jpg")
    full_plan = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "tv",
        "output_dir": str(td / "source"),
        "library_projection": {
            "hardlinks_enabled": False, "link_root": None,
        },
        "show": {
            "title": "识图测试", "sorttitle": "识图测试 2024-01-01 识图测试",
            "premiered": "2024-01-01", "bgm_id": 1, "anidb_aid": 2,
            "staff_status": "empty", "lockdata": True,
            "staff_audit": {"persons_checked": True, "mappable_crew_count": 0},
        },
        "episodes": [{
            "category": "normal", "season": 1, "episode": 1,
            "title": "第一话", "video_path": str(video),
            "plot_evidence": {
                "bangumi_zh": "empty", "tmdb_zh": "empty",
                "bangumi_ja": "empty", "tmdb_en": "empty",
            },
        }],
        "artwork_review": plan_review,
        "artwork": [
            {
                "scope": "season", "kind": "poster", "method": "tmdb",
                "url": selected["url"], "source_path": str(td / "source" / "poster.jpg"),
            },
            {
                "scope": "episode", "kind": "episode_thumb", "method": "frame",
                "fallback_video_path": str(video), "source_path": str(thumb),
            },
        ],
    }
    plan_path = td / "visual-plan.json"
    report_path = td / "visual-report.json"
    config_file = td / "config.json"
    config = load_test_config()
    config["artwork"]["multimodal_review"] = True
    write_test_config(config_file, config)
    plan_path.write_text(json.dumps(full_plan, ensure_ascii=False), encoding="utf-8")
    with config_root(td):
        with contextlib.redirect_stdout(io.StringIO()):
            assert scrape.main([
                "--plan", str(plan_path), "--dry-run", "--no-hardlinks",
                "--report-file", str(report_path)
            ]) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["validations"]["artwork_visual_review"] == "passed"

    plan["artwork"][0]["url"] = "https://image.tmdb.org/t/p/original/not-reviewed.jpg"
    try:
        scrape._validate_artwork_review(
            plan, plan["artwork"], multimodal_enabled=True
        )
        raise AssertionError("未通过 Agent 识图的 poster 不得进入 plan.artwork")
    except ValueError as exc:
        assert "未命中 Agent 识图选择" in str(exc)

    single_url = "https://image.tmdb.org/t/p/original/single.jpg"
    single_request = {"series_name": "单候选测试", "groups": [{
        "group_id": "single",
        "label": "Single",
        "work_name": "Single",
        "candidates": [{
            "file_path": "/single.jpg", "url": single_url,
            "width": 1000, "height": 1400,
        }],
        "cache_candidates": [{
            "file_path": "/single.jpg", "url": single_url,
            "width": 1000, "height": 1400,
        }],
    }]}

    def unexpected_loader(_item: dict) -> bytes:
        raise AssertionError("单候选不应下载预览")

    single_review = artwork_review.build_review(
        single_request, td / "single", loader=unexpected_loader,
        multimodal_enabled=True,
    )
    assert single_review["status"] == "not_required"
    assert single_review["reason"] == "single_candidate"
    assert single_review["sheets"] == []
    compact_single = artwork_review.compact_plan_review(single_review)
    single_plan = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "artwork_review": compact_single,
        "artwork": [{"kind": "poster", "method": "tmdb", "url": single_url}],
    }
    assert scrape._validate_artwork_review(
        single_plan, single_plan["artwork"], multimodal_enabled=True
    )["status"] == "not_required"
    single_plan["artwork"][0]["url"] = selected["url"]
    try:
        scrape._validate_artwork_review(
            single_plan, single_plan["artwork"], multimodal_enabled=True
        )
        raise AssertionError("单候选 plan 也必须命中唯一候选 URL")
    except ValueError as exc:
        assert "未命中 Agent 识图选择" in str(exc)

    missing_review_plan = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "artwork": [{"kind": "poster", "method": "tmdb", "url": single_url}],
    }
    try:
        scrape._validate_artwork_review(
            missing_review_plan, missing_review_plan["artwork"],
            multimodal_enabled=True,
        )
        raise AssertionError("plan 缺失 artwork_review 必须失败")
    except ValueError as exc:
        assert "必须声明 artwork_review" in str(exc)

    try:
        artwork_review.build_review(
            request_payload, td / "disabled-missing-selection",
            loader=unexpected_loader, multimodal_enabled=False,
        )
        raise AssertionError("关闭多模态时不得绕过原有确定性选图结果")
    except ValueError as exc:
        assert "deterministic_selection" in str(exc)

    disabled_request = json.loads(json.dumps(request_payload))
    for group in disabled_request["groups"]:
        group["deterministic_selection"] = group["candidates"][-1]
    disabled_review = artwork_review.build_review(
        disabled_request, td / "disabled", loader=unexpected_loader,
        multimodal_enabled=False,
    )
    assert disabled_review["status"] == "disabled"
    assert disabled_review["reason"] == "config_disabled"
    assert disabled_review["multimodal_review_enabled"] is False
    assert disabled_review["selection_method"] == "deterministic_existing_pipeline"
    assert disabled_review["sheets"] == []
    assert all(
        selection["candidate_id"] == group["candidates"][0]["candidate_id"]
        for selection, group in zip(
            disabled_review["selections"], disabled_review["groups"]
        )
    )
    assert [group["candidates"][0]["url"] for group in disabled_review["groups"]] == [
        group["deterministic_selection"]["url"] for group in disabled_request["groups"]
    ]
    disabled_input = td / "disabled-input.json"
    disabled_config = td / "config.json"
    disabled_cli_dir = td / "disabled-cli"
    disabled_input.write_text(
        json.dumps(disabled_request, ensure_ascii=False), encoding="utf-8"
    )
    disabled_config_payload = load_test_config()
    disabled_config_payload["artwork"]["multimodal_review"] = False
    write_test_config(disabled_config, disabled_config_payload)
    with config_root(td):
        with contextlib.redirect_stdout(io.StringIO()):
            assert artwork_review.main([
                "--input", str(disabled_input), "--output-dir", str(disabled_cli_dir)
            ]) == 0
    assert json.loads(
        (disabled_cli_dir / "artwork-review.json").read_text(encoding="utf-8")
    )["status"] == "disabled"
    assert not list(disabled_cli_dir.glob("*.jpg"))
    assert not (td / "cache" / "artwork-originals").exists()

    enabled_config_payload = load_test_config()
    enabled_config_payload["artwork"]["multimodal_review"] = False
    enabled_config_payload["artwork"]["artwork_cache"] = True
    write_test_config(disabled_config, enabled_config_payload)
    enabled_cli_dir = td / "enabled-cli"

    original_downloader = artwork_review._download_original_bytes

    def fake_original_downloader(item: dict):
        index = int(re.search(r"c(\d+)", item["file_path"]).group(1))
        source = artwork_review.Image.new(
            "RGB", (100 + index, 140 + index), (20, 60 + index, 100)
        )
        payload = io.BytesIO()
        source.save(payload, "JPEG")
        return payload.getvalue(), source.size

    artwork_review._download_original_bytes = fake_original_downloader
    try:
        with config_root(td):
            with contextlib.redirect_stdout(io.StringIO()):
                assert artwork_review.main([
                    "--input", str(disabled_input), "--output-dir", str(enabled_cli_dir)
                ]) == 0
    finally:
        artwork_review._download_original_bytes = original_downloader
    enabled_manifest = json.loads(
        (enabled_cli_dir / "artwork-review.json").read_text(encoding="utf-8")
    )
    assert enabled_manifest["original_cache"]["cached_count"] == 24
    enabled_cache_dir = Path(enabled_manifest["original_cache_dir"])
    assert enabled_cache_dir.parent.resolve() == (td / "cache" / "artwork-originals").resolve()
    assert len(list(enabled_cache_dir.glob("*.jpg"))) == 24

    compact_disabled = artwork_review.compact_plan_review(disabled_review)
    disabled_selected = disabled_review["groups"][0]["candidates"][0]
    disabled_plan = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "library_projection": {
            "hardlinks_enabled": False, "link_root": None,
        },
        "artwork_review": compact_disabled,
        "artwork": [{
            "kind": "poster", "method": "tmdb", "url": disabled_selected["url"],
        }],
    }
    assert scrape._validate_artwork_review(
        disabled_plan, disabled_plan["artwork"], multimodal_enabled=False
    )["status"] == "disabled"
    try:
        scrape._validate_artwork_review(
            disabled_plan, disabled_plan["artwork"], multimodal_enabled=True
        )
        raise AssertionError("plan 识图开关与当前 config 不一致时必须失败")
    except ValueError as exc:
        assert "与当前 config" in str(exc)

    disabled_full_plan = json.loads(json.dumps(full_plan))
    disabled_full_plan["artwork_review"] = compact_disabled
    disabled_full_plan["artwork"][0]["url"] = disabled_selected["url"]
    disabled_plan_path = td / "disabled-plan.json"
    disabled_report_path = td / "disabled-report.json"
    disabled_plan_path.write_text(
        json.dumps(disabled_full_plan, ensure_ascii=False), encoding="utf-8"
    )
    with config_root(td):
        with contextlib.redirect_stdout(io.StringIO()):
            assert scrape.main([
                "--plan", str(disabled_plan_path), "--dry-run", "--no-hardlinks",
                "--report-file", str(disabled_report_path),
            ]) == 0
        assert json.loads(
            disabled_report_path.read_text(encoding="utf-8")
        )["summary"]["validations"]["artwork_visual_review"] == "disabled"

        disabled_config_payload["artwork"]["multimodal_review"] = True
        write_test_config(disabled_config, disabled_config_payload)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                scrape.main([
                    "--plan", str(disabled_plan_path), "--dry-run", "--no-hardlinks",
                    "--report-file", str(disabled_report_path),
                ])
            raise AssertionError("scrape CLI 必须拒绝 config 与 plan 开关不一致")
        except ValueError as exc:
            assert "与当前 config" in str(exc)
    print("  [ok] Agent 海报识图候选、320x480 等比补边与 plan 选择护栏正确")

def test_artwork_projection(td: Path):
    """图片实体只落源目录,库侧图片必须是硬链接。"""
    td.mkdir(parents=True, exist_ok=True)
    write_test_config(td / "config.json")
    src = td / "src"
    video = _touch(src / "ep01.mkv")
    source_image = _write_test_image(src / "ep01-thumb.jpg")
    poster = _write_test_image(src / "poster.jpg", color=(120, 80, 60))
    specials_poster = _write_test_image(src / "specials-poster.jpg", color=(60, 120, 80))
    plan = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "tv", "output_dir": str(src),
        "library_projection": {"hardlinks_enabled": True, "link_root": None},
        "multimodal_review_enabled": False,
        "artwork_review": {
            "schema": "anime-scraper-artwork-review-v1",
            "status": "disabled",
            "multimodal_review_enabled": False,
            "selection_method": "deterministic_existing_pipeline",
            "preview": {"width": 320, "height": 480},
            "candidate_limit": 5,
            "source_manifest_sha256": "0" * 64,
            "groups": [],
        },
        "show": {"title": "图片测试", "sorttitle": "图片测试 2024-01-01 图片测试",
                 "premiered": "2024-01-01", "lockdata": True},
        "episodes": [{"category": "normal", "season": 1, "episode": 1,
                      "title": "第一话", "video_path": str(video),
                      "plot_evidence": {
                          "bangumi_zh": "empty", "tmdb_zh": "empty",
                          "bangumi_ja": "empty", "tmdb_en": "empty",
                      }}],
        "artwork": [
            {"scope": "episode", "kind": "episode_thumb",
             "source_path": str(source_image),
             "library_relpath": "Season 01/图片测试 S01E01-thumb.jpg",
             "method": "tmdb", "url": "https://example.invalid/not-used"},
            # 同一源实体可链到库侧多处
            {"scope": "show", "kind": "poster",
             "source_path": str(poster),
             "library_relpath": "poster.jpg",
             "method": "tmdb", "url": "https://example.invalid/not-used"},
            {"scope": "season", "kind": "poster",
             "source_path": str(poster),
             "library_relpath": "Season 01/poster.jpg",
             "method": "tmdb", "url": "https://example.invalid/not-used"},
            # Season 0 有专属/主池备用图时，使用独立实体投影到 Specials。
            {"scope": "season", "kind": "poster",
             "source_path": str(specials_poster),
             "library_relpath": "Specials/poster.jpg",
             "method": "tmdb", "url": "https://example.invalid/specials-poster"},
        ],
    }
    plan_file = td / "plan.json"
    plan_file.write_text(__import__("json").dumps(plan, ensure_ascii=False), encoding="utf-8")
    library = td / "library"
    plan["library_projection"]["link_root"] = str(library)
    plan_file.write_text(__import__("json").dumps(plan, ensure_ascii=False), encoding="utf-8")
    with config_root(td):
        assert scrape.main(["--plan", str(plan_file), "--link-root", str(library)]) == 0
    linked = library / "图片测试 (2024)" / "Season 01" / "图片测试 S01E01-thumb.jpg"
    assert linked.exists() and images.verify_hardlink(source_image, linked)
    root_poster = library / "图片测试 (2024)" / "poster.jpg"
    season_poster = library / "图片测试 (2024)" / "Season 01" / "poster.jpg"
    specials_link = library / "图片测试 (2024)" / "Specials" / "poster.jpg"
    assert images.verify_hardlink(poster, root_poster)
    assert images.verify_hardlink(poster, season_poster)
    assert images.verify_hardlink(specials_poster, specials_link)

    # 复用主图作为 Specials 海报必须被拒绝。
    reuse_src = td / "reuse-src"
    reuse_video = _touch(reuse_src / "ep01.mkv")
    reuse_thumb = _touch(reuse_src / "ep01-thumb.jpg")
    reuse_poster = _write_test_image(reuse_src / "poster.jpg")
    reuse_plan = {
        "type": "tv", "output_dir": str(reuse_src),
        "show": {"title": "复用海报", "premiered": "2024-01-01"},
        "episodes": [{"category": "normal", "season": 1, "episode": 1,
                      "title": "第一话", "video_path": str(reuse_video)}],
        "artwork": [
            {"scope": "episode", "kind": "episode_thumb",
             "source_path": str(reuse_thumb),
             "library_relpath": "Season 01/复用海报 S01E01-thumb.jpg",
             "method": "frame", "fallback_video_path": str(reuse_video)},
            {"scope": "show", "kind": "poster", "source_path": str(reuse_poster),
             "library_relpath": "poster.jpg", "method": "tmdb", "url": "https://example.invalid/not-used"},
            {"scope": "season", "kind": "poster", "source_path": str(reuse_poster),
             "library_relpath": "Season 01/poster.jpg", "method": "tmdb", "url": "https://example.invalid/not-used"},
            {"scope": "season", "kind": "poster", "source_path": str(reuse_poster),
             "library_relpath": "Specials/poster.jpg", "method": "tmdb", "url": "https://example.invalid/not-used"},
        ],
    }
    reuse_plan_file = td / "reuse-plan.json"
    reuse_plan_file.write_text(__import__("json").dumps(reuse_plan, ensure_ascii=False), encoding="utf-8")
    reuse_library = td / "reuse-library"
    with config_root(td):
        try:
            scrape.main(["--plan", str(reuse_plan_file), "--link-root", str(reuse_library)])
            raise AssertionError("Specials/poster.jpg 复用主图必须被拒绝")
        except ValueError as exc:
            assert "plan_schema" in str(exc) or "不得复用" in str(exc)
    assert not reuse_library.exists(), "拒绝复用主图时不得创建库目录"
    assert not list(library.rglob("*.download-*"))
    print("  [ok] artwork 保持源目录唯一实体，Specials 海报独立且禁止复用主图")

def test_artwork_original_cache(td: Path):
    """原图缓存过滤候选，并能按候选编号完成本地替换。"""
    candidates = [
        _image_candidate(f"/cache-{index}.jpg", language="ja", votes=index,
                         width=1000 + index, height=1500 + index)
        for index in range(1, 8)
    ] + [
        _image_candidate("/cache-08-no-votes.jpg", language="zh", votes=0),
        _image_candidate("/cache-09-korean.jpg", language="ko", votes=8),
        _image_candidate("/cache-10-french.jpg", language="fr", votes=9),
        _image_candidate("/cache-11-zh-cn.jpg", language="zh-CN", votes=10),
        _image_candidate("/cache-12-zh-tw.jpg", language="zh-TW", votes=11),
        _image_candidate("/cache-13-en.jpg", language="en", votes=12),
    ]
    request = {"series_name": "咒术回战", "groups": [{
        "group_id": "season-01",
        "label": "咒术回战 - 第一单元 - 主海报",
        "work_name": "第一单元",
        # Only five candidates are exposed to disabled/visual review, while
        # caching must still see the complete source pool.
        "candidates": candidates[:5], "cache_candidates": candidates,
        "deterministic_selection": candidates[-1],
    }]}

    assert artwork_cache.compact_work_label("第一单元") == "s1"
    assert artwork_cache.compact_work_label("第二季") == "s2"
    assert artwork_cache.compact_work_label("剧场版") == "movie"

    def loader(candidate: dict) -> tuple[bytes, tuple[int, int]]:
        index = int(re.search(r"cache-(\d+)", candidate["file_path"]).group(1))
        image = artwork_review.Image.new("RGB", (100 + index, 150 + index),
                                         (index * 20, 40, 80))
        payload = io.BytesIO()
        image.save(payload, "JPEG")
        return payload.getvalue(), (100 + index, 150 + index)

    original_decode = artwork_cache.decode_image_size
    verify_calls = 0

    def counting_verify(source):
        nonlocal verify_calls
        verify_calls += 1
        return original_decode(source)

    artwork_cache.decode_image_size = counting_verify
    try:
        review = artwork_review.build_review(
            request, td / "review", multimodal_enabled=False,
            original_cache_root=td / "cache", original_loader=loader,
        )
    finally:
        artwork_cache.decode_image_size = original_decode
    assert verify_calls == 0, "已验证尺寸的原图缓存不应再次完整解码"
    cache_dir = Path(review["original_cache_dir"])
    assert review["groups"][0]["cache_candidate_count"] == len(candidates)
    assert len(review["groups"][0]["candidates"]) == 1
    try:
        artwork_review.build_review(
            {"groups": [{
                "group_id": "season-missing-cache",
                "label": "咒术回战 - 第一单元",
                "work_name": "第一单元",
                "candidates": candidates[:5],
                "deterministic_selection": candidates[0],
            }]},
            td / "missing-cache-review", multimodal_enabled=False,
            original_cache_root=td / "missing-cache", original_loader=loader,
        )
        raise AssertionError("1.0 artwork review 必须要求 cache_candidates")
    except ValueError as exc:
        assert "cache_candidates" in str(exc)
    cache_manifest_path = cache_dir / artwork_cache.MANIFEST_NAME
    cache_manifest = json.loads(cache_manifest_path.read_text(encoding="utf-8"))
    cached_entries = cache_manifest["groups"][0]["candidates"]
    cached_ids = [candidate["candidate_id"] for candidate in cached_entries]
    cached_paths = {candidate["file_path"] for candidate in cached_entries}
    expected_paths = {
        candidate["file_path"] for candidate in candidates
        if candidate["vote_count"] > 0
        and candidate["language"].casefold() in artwork_cache.CACHE_ALLOWED_LANGUAGES
    }
    initial_current_id = cache_manifest["groups"][0]["current_candidate_id"]
    assert cached_paths == expected_paths
    assert len(cached_ids) == len(expected_paths) == 10
    assert all(
        entry["vote_count"] > 0
        and entry["language"].casefold() in artwork_cache.CACHE_ALLOWED_LANGUAGES
        for entry in cached_entries
    )
    assert cache_dir.name.startswith("咒术回战-")
    assert "+" not in cache_dir.name
    assert len(list(cache_dir.glob("*.jpg"))) == 10
    assert any(path.name.startswith("咒术回战 - s1 - G01-C01")
               for path in cache_dir.glob("*.jpg"))
    assert all("主海报" not in path.name for path in cache_dir.glob("*.jpg"))
    assert any(f"{initial_current_id} - CURRENT" in path.name
               for path in cache_dir.glob("*.jpg"))
    assert all("artwork-original" not in path.name for path in cache_dir.glob("*.jpg"))
    resolved = artwork_cache.resolve_candidate(cache_dir, candidate_id="G01-C11")
    assert resolved["cache_path"].is_file()

    source = _write_test_image(td / "source" / "poster.jpg", color=(1, 2, 3))
    library = td / "library" / "poster.jpg"
    library.parent.mkdir(parents=True)
    os.link(source, library)
    old_url = "https://image.tmdb.org/t/p/original/old.jpg"
    plan_path = td / "plan.json"
    plan_path.write_text(json.dumps({
        "artwork_review": {"original_cache_dir": str(cache_dir)},
        "artwork": [{"kind": "poster", "source_path": str(source),
                     "method": "tmdb", "url": old_url}],
    }, ensure_ascii=False), encoding="utf-8")
    change_set = {
        "schema": update_artwork.SCHEMA, "mode": "incremental",
        "budget": {"downloads": 0, "ffmpeg": 0},
        "items": [{
            "candidate_id": "G01-C11", "original_cache_dir": str(cache_dir),
            "kind": "poster", "new_method": "tmdb", "old_method": "tmdb",
            "old_url": old_url, "plan_path": str(plan_path),
            "source_path": str(source), "library_paths": [str(library)],
            "expected_old_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }],
    }
    result = update_artwork.apply_change_set(change_set)
    assert result["updated"] == 1 and images.verify_hardlink(source, library)
    updated_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert updated_plan["artwork"][0]["candidate_id"] == "G01-C11"
    assert updated_plan["artwork"][0]["url"] == resolved["url"]
    current_manifest = json.loads(cache_manifest_path.read_text(encoding="utf-8"))
    assert any("G01-C11 - CURRENT" in path.name for path in cache_dir.glob("*.jpg"))
    assert not any(f"{initial_current_id} - CURRENT" in path.name
                   for path in cache_dir.glob("*.jpg"))

    shared = _image_candidate("/shared.jpg", language="en", votes=2)
    main = _image_candidate("/main.jpg", language="en", votes=3)
    other = _image_candidate("/other.jpg", language="en", votes=4)

    def duplicate_loader(_candidate: dict) -> tuple[bytes, tuple[int, int]]:
        image = artwork_review.Image.new("RGB", (100, 150), (12, 34, 56))
        payload = io.BytesIO()
        image.save(payload, "JPEG")
        return payload.getvalue(), (100, 150)

    duplicate_cache = td / "duplicate-cache"
    artwork_cache.cache_originals(
        duplicate_cache,
        [{
            "group_id": "main-posters", "label": "主图", "work_name": "第一季",
            "current_candidate_id": "G01-C01",
            "cache_candidates": [
                {**main, "candidate_id": "G01-C01"},
                {**shared, "candidate_id": "G01-C02"},
            ],
        }, {
            "group_id": "specials-posters", "label": "Specials",
            "work_name": "Specials", "current_candidate_id": "G02-C02",
            "cache_candidates": [
                {**shared, "candidate_id": "G02-C01"},
                {**other, "candidate_id": "G02-C02"},
            ],
        }],
        loader=duplicate_loader,
        series_name="重复候选测试",
    )
    artwork_cache.sync_current_markers(
        duplicate_cache, [{"candidate_id": "G02-C01"}]
    )
    duplicate_manifest = json.loads(
        (duplicate_cache / artwork_cache.MANIFEST_NAME).read_text(encoding="utf-8")
    )
    selected_shared = next(
        candidate for group in duplicate_manifest["groups"]
        for candidate in group["candidates"]
        if candidate["candidate_id"] == "G02-C01"
    )
    assert " - CURRENT" in Path(selected_shared["cache_path"]).stem
    assert len(list(duplicate_cache.glob("* - CURRENT.jpg"))) == 2
    print("  [ok] 共享 TMDB file_path 的主图/Specials CURRENT 标记不会互相覆盖")

    stale = td / "cache" / "stale"
    stale.mkdir()
    stale_manifest = {
        "schema": artwork_cache.SCHEMA, "status": "completed",
        "created_at": artwork_cache._iso(
            artwork_cache._utc_now() - artwork_cache.TTL
            - artwork_cache.timedelta(hours=1)
        ),
    }
    (stale / artwork_cache.MANIFEST_NAME).write_text(
        json.dumps(stale_manifest), encoding="utf-8"
    )
    unknown = td / "cache" / "unknown"
    unknown.mkdir()
    artwork_cache.cleanup_expired(td / "cache")
    assert not stale.exists() and unknown.exists()
    print("  [ok] 原图缓存系列/作品短名、语言票数过滤、CURRENT 标记、候选编号替换与 7 天懒清理正确")


def test_incremental_artwork_update(td: Path):
    """增量海报执行器只改白名单目标，并重建源库硬链接。"""
    source = td / "source" / "poster.jpg"
    source.parent.mkdir(parents=True)
    old_image = artwork_review.Image.new("RGB", (80, 120), (20, 40, 60))
    old_image.save(source, "JPEG")
    library_root = td / "library"
    library_root.mkdir()
    root_poster = library_root / "poster.jpg"
    season_dir = library_root / "Season 01"
    season_dir.mkdir()
    season_poster = season_dir / "poster.jpg"
    os.link(source, root_poster)
    os.link(source, season_poster)
    untouched = td / "untouched.jpg"
    old_image.save(untouched, "JPEG")
    untouched_hash = hashlib.sha256(untouched.read_bytes()).hexdigest()

    old_url = "https://image.tmdb.org/t/p/original/old.jpg"
    new_url = "https://image.tmdb.org/t/p/original/new.jpg"
    plan_path = td / "plan.json"
    plan = {
        "artwork_review": {"old": True},
        "artwork": [
            {"kind": "poster", "source_path": str(source),
             "library_relpath": "poster.jpg", "method": "tmdb", "url": old_url},
            {"kind": "poster", "source_path": str(source),
             "library_relpath": "Season 01/poster.jpg", "method": "tmdb", "url": old_url},
            {"kind": "poster", "source_path": str(td / "specials-poster.jpg"),
             "library_relpath": "Specials/poster.jpg", "method": "tmdb",
             "url": "specials-unchanged"},
        ],
    }
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    review_path = td / "review.json"
    review = {"schema": "anime-scraper-artwork-review-v1", "status": "completed"}
    review_path.write_text(json.dumps(review), encoding="utf-8")

    new_image = artwork_review.Image.new("RGB", (100, 150), (100, 30, 20))
    buffer = io.BytesIO()
    new_image.save(buffer, "JPEG")
    change_set = {
        "schema": update_artwork.SCHEMA,
        "mode": "incremental",
        "review_path": str(review_path),
        "budget": {"downloads": 1},
        "items": [{
            "plan_path": str(plan_path),
            "source_path": str(source),
            "library_paths": [str(root_poster), str(season_poster)],
            "old_url": old_url,
            "url": new_url,
            "old_method": "tmdb",
            "new_method": "tmdb",
            "candidate_width": 999,
            "candidate_height": 999,
            "expected_old_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }],
    }
    result = update_artwork.apply_change_set(
        change_set, loader=lambda _url: buffer.getvalue()
    )
    assert result["status"] == "completed"
    assert result["updated"] == 1 and result["resumed"] == 0
    assert result["items"][0]["width"] == 100
    assert result["items"][0]["height"] == 150
    assert result["items"][0]["candidate_width"] == 999
    assert result["items"][0]["candidate_height"] == 999
    assert images.verify_hardlink(source, root_poster)
    assert images.verify_hardlink(source, season_poster)
    updated_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert updated_plan["artwork_review"] == review
    assert [item["url"] for item in updated_plan["artwork"][:2]] == [new_url, new_url]
    assert updated_plan["artwork"][2]["url"] == "specials-unchanged"
    assert hashlib.sha256(untouched.read_bytes()).hexdigest() == untouched_hash
    resumed = update_artwork.apply_change_set(
        change_set,
        loader=lambda _url: (_ for _ in ()).throw(
            AssertionError("已完成 change set 不得重复下载")
        ),
        resume_report=result,
    )
    assert resumed["updated"] == 0 and resumed["resumed"] == 1
    assert resumed["items"][0]["action"] == "already_completed"
    assert images.verify_hardlink(source, root_poster)
    assert images.verify_hardlink(source, season_poster)

    specials_source = td / "specials-poster.jpg"
    old_image.save(specials_source, "JPEG")
    specials_dir = library_root / "Specials"
    specials_dir.mkdir()
    specials_destination = specials_dir / "poster.jpg"
    os.link(specials_source, specials_destination)
    specials_old_url = "https://image.tmdb.org/t/p/original/specials-old.jpg"
    specials_new_url = "https://image.tmdb.org/t/p/original/specials-new.jpg"
    updated_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    updated_plan["artwork"][2]["url"] = specials_old_url
    updated_plan["artwork"][2]["specials_selection"] = "season_zero"
    plan_path.write_text(json.dumps(updated_plan, ensure_ascii=False), encoding="utf-8")
    specials_change_set = {
        "schema": update_artwork.SCHEMA,
        "mode": "incremental",
        "review_path": str(review_path),
        "budget": {"downloads": 1, "ffmpeg": 0},
        "items": [{
            "operation": "replace",
            "kind": "poster",
            "library_relpath": "Specials/poster.jpg",
            "source_path": str(specials_source),
            "library_paths": [str(specials_destination)],
            "plan_path": str(plan_path),
            "old_method": "tmdb",
            "old_url": specials_old_url,
            "new_method": "tmdb",
            "url": specials_new_url,
            "candidate_width": 100,
            "candidate_height": 150,
            "specials_selection": "season_zero",
            "expected_old_sha256": hashlib.sha256(specials_source.read_bytes()).hexdigest(),
        }],
    }
    specials_result = update_artwork.apply_change_set(
        specials_change_set, loader=lambda _url: buffer.getvalue()
    )
    assert specials_result["status"] == "completed"
    assert specials_result["updated"] == 1
    assert images.verify_hardlink(specials_source, specials_destination)
    final_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert final_plan["artwork"][2]["url"] == specials_new_url
    assert final_plan["artwork"][2]["specials_selection"] == "season_zero"

    add_source = td / "new-specials" / "specials-poster.jpg"
    add_source.parent.mkdir(parents=True)
    add_library = td / "new-specials-library" / "Specials"
    add_library.mkdir(parents=True)
    add_destination = add_library / "poster.jpg"
    add_plan_path = td / "new-specials-plan.json"
    add_main_source = td / "new-specials" / "poster.jpg"
    new_image.save(add_main_source, "JPEG")
    add_url = "https://image.tmdb.org/t/p/original/specials-new.jpg"
    add_review = {
        "schema": "anime-scraper-artwork-review-v1",
        "status": "completed",
        "groups": [{"group_id": "season-01", "candidates": [{"url": add_url}]}],
    }
    add_review_path = td / "new-specials-review.json"
    add_review_path.write_text(json.dumps(add_review), encoding="utf-8")
    add_plan_path.write_text(json.dumps({
        "artwork_review": {"old": True},
        "artwork": [{
            "kind": "poster", "source_path": str(add_main_source),
            "library_relpath": "poster.jpg", "url": "https://image.tmdb.org/t/p/original/main.jpg",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    add_change_set = {
        "schema": update_artwork.SCHEMA,
        "mode": "incremental",
        "review_path": str(add_review_path),
        "budget": {"downloads": 1, "ffmpeg": 0},
        "items": [{
            "operation": "add",
            "plan_path": str(add_plan_path),
            "source_path": str(add_source),
            "library_paths": [str(add_destination)],
            "scope": "season",
            "kind": "poster",
            "library_relpath": "Specials/poster.jpg",
            "specials_selection": "season_zero",
            "new_method": "tmdb",
            "url": add_url,
            # 新字段优先于旧字段，二者都只作为候选 hint。
            "candidate_width": 999,
            "candidate_height": 999,
            "width": 1,
            "height": 1,
        }],
    }
    added = update_artwork.apply_change_set(
        add_change_set, loader=lambda _url: buffer.getvalue()
    )
    assert added["updated"] == 1 and add_source.is_file()
    assert images.verify_hardlink(add_source, add_destination)
    added_plan = json.loads(add_plan_path.read_text(encoding="utf-8"))
    added_specials = [item for item in added_plan["artwork"]
                      if item.get("library_relpath") == "Specials/poster.jpg"]
    assert len(added_specials) == 1
    assert added_specials[0]["url"] == add_url
    assert added_specials[0]["specials_selection"] == "season_zero"

    remove_reason = "Season 0 候选与主海报视觉内容基本一致，按规则省略 Specials 海报并回退主海报。"
    remove_change_set = {
        "schema": update_artwork.SCHEMA,
        "mode": "incremental",
        "budget": {"downloads": 0, "ffmpeg": 0},
        "items": [{
            "operation": "remove",
            "plan_path": str(add_plan_path),
            "source_path": str(add_source),
            "library_paths": [str(add_destination)],
            "kind": "poster",
            "library_relpath": "Specials/poster.jpg",
            "old_url": add_url,
            "old_specials_selection": "season_zero",
            "expected_old_sha256": hashlib.sha256(add_source.read_bytes()).hexdigest(),
            "removal_reason": remove_reason,
        }],
    }
    removed = update_artwork.apply_change_set(remove_change_set)
    assert removed["removed"] == 1
    assert not add_source.exists() and not add_destination.exists()
    removed_plan = json.loads(add_plan_path.read_text(encoding="utf-8"))
    assert not [item for item in removed_plan["artwork"]
                if item.get("library_relpath") == "Specials/poster.jpg"]
    assert removed_plan["_meta"]["specials_selection"] == "none"
    assert removed_plan["_meta"]["specials_selection_reason"] == remove_reason
    resumed_remove = update_artwork.apply_change_set(
        remove_change_set, resume_report=removed
    )
    assert resumed_remove["removed"] == 0 and resumed_remove["resumed"] == 1
    print("  [ok] 增量海报替换、新增、撤销、plan 同步、硬链接与中断恢复正确")

    frame_source = td / "frame-source" / "episode-thumb.jpg"
    frame_video = td / "frame-source" / "episode.mkv"
    frame_source2 = td / "frame-source" / "episode2-thumb.jpg"
    frame_video2 = td / "frame-source" / "episode2.mkv"
    frame_source.parent.mkdir(parents=True)
    old_frame = artwork_review.Image.new("RGB", (80, 120), (12, 34, 56))
    old_frame.save(frame_source, "JPEG")
    frame_video.write_bytes(b"test video placeholder")
    old_frame.save(frame_source2, "JPEG")
    frame_video2.write_bytes(b"test video placeholder 2")
    frame_library = td / "frame-library" / "Specials" / "Show S00E01-thumb.jpg"
    frame_library2 = td / "frame-library" / "Specials" / "Show S00E02-thumb.jpg"
    frame_library.parent.mkdir(parents=True)
    os.link(frame_source, frame_library)
    os.link(frame_source2, frame_library2)
    frame_old_url = "https://image.tmdb.org/t/p/original/wrong-still.jpg"
    frame_old_url2 = "https://image.tmdb.org/t/p/original/wrong-still-2.jpg"
    frame_plan_path = td / "frame-plan.json"
    frame_plan = {
        "artwork_review": {"unchanged": True},
        "episodes": [{
            "season": 0, "episode": 1, "title": "科学讲座 1",
            "video_path": str(frame_video), "tmdb_match_status": "matched",
            "tmdb_still_url": frame_old_url,
        }, {
            "season": 0, "episode": 2, "title": "科学讲座 2",
            "video_path": str(frame_video2), "tmdb_match_status": "matched",
            "tmdb_still_url": frame_old_url2,
        }],
        "artwork": [{
            "kind": "episode_thumb", "source_path": str(frame_source),
            "library_relpath": "Specials/Show S00E01-thumb.jpg",
            "method": "tmdb", "url": frame_old_url,
        }, {
            "kind": "episode_thumb", "source_path": str(frame_source2),
            "library_relpath": "Specials/Show S00E02-thumb.jpg",
            "method": "tmdb", "url": frame_old_url2,
        }],
    }
    frame_plan_path.write_text(json.dumps(frame_plan, ensure_ascii=False), encoding="utf-8")
    frame_image = artwork_review.Image.new("RGB", (1920, 1080), (180, 90, 20))
    original_ffmpeg_thumb = update_artwork.images.ffmpeg_thumb

    def fake_ffmpeg_thumb(_video, thumb, *, skip_existing=True, timeout=60,
                          return_size=False):
        frame_image.save(thumb, "JPEG")
        return (1920, 1080) if return_size else True

    update_artwork.images.ffmpeg_thumb = fake_ffmpeg_thumb
    try:
        frame_change_set = {
            "schema": update_artwork.SCHEMA,
            "mode": "incremental",
            "budget": {"downloads": 0, "ffmpeg": 2},
            "items": [{
                "kind": "episode_thumb", "new_method": "frame",
                "plan_path": str(frame_plan_path),
                "source_path": str(frame_source),
                "library_paths": [str(frame_library)],
                "old_method": "tmdb", "old_url": frame_old_url,
                "fallback_video_path": str(frame_video),
                "new_tmdb_match_status": "unknown",
                "expected_old_sha256": hashlib.sha256(frame_source.read_bytes()).hexdigest(),
            }, {
                "kind": "episode_thumb", "new_method": "frame",
                "plan_path": str(frame_plan_path),
                "source_path": str(frame_source2),
                "library_paths": [str(frame_library2)],
                "old_method": "tmdb", "old_url": frame_old_url2,
                "fallback_video_path": str(frame_video2),
                "new_tmdb_match_status": "unknown",
                "expected_old_sha256": hashlib.sha256(frame_source2.read_bytes()).hexdigest(),
            }],
        }
        frame_result = update_artwork.apply_change_set(frame_change_set)
        assert frame_result["status"] == "completed"
        assert frame_result["updated"] == 2 and frame_result["resumed"] == 0
        assert all(
            item["width"] == 1920 and item["height"] == 1080
            for item in frame_result["items"]
        )
        assert images.verify_hardlink(frame_source, frame_library)
        assert images.verify_hardlink(frame_source2, frame_library2)
        updated_frame_plan = json.loads(frame_plan_path.read_text(encoding="utf-8"))
        for index, video in enumerate((frame_video, frame_video2)):
            assert updated_frame_plan["episodes"][index]["tmdb_match_status"] == "unknown"
            assert "tmdb_still_url" not in updated_frame_plan["episodes"][index]
            updated_frame_artwork = updated_frame_plan["artwork"][index]
            assert updated_frame_artwork["method"] == "frame"
            assert "url" not in updated_frame_artwork
            assert Path(updated_frame_artwork["fallback_video_path"]).resolve() == video.resolve()
        resumed_frame = update_artwork.apply_change_set(
            frame_change_set,
            loader=lambda _url: (_ for _ in ()).throw(
                AssertionError("本地截帧恢复不得下载远程图片")
            ),
            resume_report=frame_result,
        )
        assert resumed_frame["updated"] == 0 and resumed_frame["resumed"] == 2
        assert all(item["action"] == "already_completed"
                   for item in resumed_frame["items"])
    finally:
        update_artwork.images.ffmpeg_thumb = original_ffmpeg_thumb
    print("  [ok] 增量本地截帧替换、TMDB 证据清理、硬链接与中断恢复正确")

def test_artwork_relpath_extension_guard(td: Path):
    """作品级 artwork 库侧路径缺少图片扩展名时，dry-run 必须拒绝。"""
    src = td / "src"
    video = _touch(src / "ep01.mkv")
    poster = _write_test_image(src / "poster.jpg")
    plan = {
        "type": "tv", "output_dir": str(src),
        "show": {"title": "扩展名护栏", "premiered": "2024-01-01"},
        "episodes": [{"category": "normal", "season": 1, "episode": 1,
                      "title": "第一话", "video_path": str(video)}],
        "artwork": [{"scope": "show", "kind": "poster",
                      "source_path": str(poster), "library_relpath": "poster",
                      "method": "tmdb", "url": "https://example.invalid/x"}],
    }
    plan_file = td / "plan.json"
    plan_file.write_text(__import__("json").dumps(plan, ensure_ascii=False), encoding="utf-8")
    try:
        scrape.main(["--plan", str(plan_file), "--dry-run", "--link-root", str(td / "lib")])
        raise AssertionError("dry-run 应拒绝无扩展名 artwork")
    except ValueError as exc:
        assert "plan_schema" in str(exc) or "图片扩展名" in str(exc), str(exc)

    # 海报位与 kind 的对应必须锁死:识图护栏和 --cached-replace 都按
    # kind=="poster" 取件,落在海报位却写别的 kind 会让这张图静默绕过审查。
    for bad_kind, relpath in (("specials-poster", "Specials/poster.jpg"),
                              ("fanart", "Season 02/poster.jpg"),
                              ("banner", "poster.png")):
        try:
            scrape._validate_artwork_relpath({"kind": bad_kind}, relpath, 0)
            raise AssertionError(f"海报位 {relpath} 的 kind={bad_kind!r} 必须被拒绝")
        except ValueError as exc:
            assert "海报位" in str(exc), str(exc)
    for relpath in ("poster.jpg", "Season 01/poster.jpg", "Specials/poster.jpg"):
        scrape._validate_artwork_relpath({"kind": "poster"}, relpath, 0)
    # 非海报位不受该规则约束。
    scrape._validate_artwork_relpath({"kind": "fanart"}, "fanart.jpg", 0)
    scrape._validate_artwork_relpath(
        {"kind": "thumb"}, "Specials/某作品 S00E01-thumb.jpg", 0)
    print("  [ok] artwork 库侧路径扩展名与海报位 kind 护栏正确")

def test_thumb_stem_guard_rejects_year_in_name(td: Path):
    """分集 thumb 带 (年) 时 preflight/dry-run 必须失败（死亡笔记/GRIDMAN 复踩）。"""
    src = td / "src"
    lib = td / "lib"
    video = _touch(src / "ep01.mkv")
    thumb = _write_test_image(src / "ep01-thumb.jpg")
    plan = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "tv", "output_dir": str(src),
        "show": {"title": "护栏番", "sorttitle": "护栏番 2018-10-06 护栏番",
                 "premiered": "2018-10-06", "lockdata": True},
        "library_projection": {"hardlinks_enabled": True, "link_root": str(lib)},
        "episodes": [{"category": "normal", "season": 1, "episode": 1,
                      "title": "第一话", "video_path": str(video)}],
        "artwork": [{
            "scope": "episode", "kind": "episode_thumb",
            "source_path": str(thumb),
            # 错：把文件夹名「护栏番 (2018)」写进 thumb；对的是「护栏番 S01E01-thumb.jpg」
            "library_relpath": "Season 01/护栏番 (2018) S01E01-thumb.jpg",
            "method": "tmdb", "url": "https://example.invalid/x",
        }],
    }
    show, vps = match.assemble(plan)
    lib = td / "lib"
    try:
        link_library.preflight_tv_tree(show, vps, str(lib), artwork=plan["artwork"])
        raise AssertionError("应拒绝带年份的分集 thumb library_relpath")
    except ValueError as exc:
        msg = str(exc)
        assert "不一致" in msg or "stem" in msg or "年份" in msg, msg
    plan_file = td / "plan.json"
    plan_file.write_text(__import__("json").dumps(plan, ensure_ascii=False), encoding="utf-8")
    # dry-run 也不得放行（preflight 抛 ValueError）
    try:
        scrape.main(["--plan", str(plan_file), "--dry-run", "--link-root", str(lib)])
        raise AssertionError("dry-run 对错误 thumb stem 应失败")
    except ValueError as exc:
        assert "plan_schema" in str(exc) or "不一致" in str(exc) or "年份" in str(exc), str(exc)
    assert not lib.exists(), "失败 dry-run 不得建库目录"
    print("  [ok] thumb stem 护栏拒绝「片名 (年) SxxExx-thumb」")

def test_special_airdate_guard(td: Path):
    """入库 Season 0 / movie extras 缺 airdate 时，组装和 dry-run 必须拒绝。"""
    src = td / "src"
    primary = _touch(src / "ep01.mkv")
    video = _touch(src / "ncop.mkv")
    plan = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "tv", "output_dir": str(src),
        "show": {"title": "日期护栏", "sorttitle": "日期护栏 2006-10-04 日期护栏",
                 "premiered": "2006-10-04", "lockdata": True},
        "library_projection": {"hardlinks_enabled": False, "link_root": None},
        "episodes": [
            {"category": "normal", "season": 1, "episode": 1,
             "title": "第一话", "video_path": str(primary)},
            {"category": "credit", "season": 0, "episode": 1,
             "title": "NCOP", "video_path": str(video),
             "song_evidence": {
                 "status": "exhausted",
                 "sources": ["official:none"],
                 "note": "测试用裸 OP"
             }},
        ],
    }
    show, video_paths = match.assemble(plan)
    try:
        match.validate_special_airdates(show, video_paths)
        raise AssertionError("应拒绝缺少 airdate 的入库 Season 0")
    except ValueError as exc:
        assert "airdate" in str(exc) and "NCOP" in str(exc), str(exc)

    plan_file = td / "plan.json"
    plan["artwork"] = []  # 空 artwork，避免触发 artwork_review 校验
    plan_file.write_text(__import__("json").dumps(plan, ensure_ascii=False), encoding="utf-8")
    try:
        scrape.main(["--plan", str(plan_file), "--dry-run", "--no-hardlinks"])
        raise AssertionError("dry-run 应拒绝缺少 airdate 的入库 Season 0")
    except ValueError as exc:
        assert "plan_schema" in str(exc) or "airdate" in str(exc), str(exc)

    # 有日期的特殊集必须放行；video_path=None 表示跳过、不生成 NFO，也允许空日期。
    plan["episodes"][1]["airdate"] = "2006-10-04"
    plan["episodes"].append({"category": "special", "season": 0, "episode": 2,
                             "title": "待人工", "video_path": None})
    valid_show, valid_paths = match.assemble(plan)
    match.validate_special_airdates(valid_show, valid_paths)

    movie_plan = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "movie", "output_dir": str(src),
        "movie": {"title": "电影日期护栏", "sorttitle": "电影日期护栏 2023-01-01 电影日期护栏",
                  "premiered": "2023-01-01"},
        "library_projection": {"hardlinks_enabled": False, "link_root": None},
        "artwork": [],
        "extras": [{"category": "credit", "season": 0, "episode": 1,
                    "title": "电影 NCOP", "video_path": str(video),
                    "song_evidence": {
                        "status": "exhausted",
                        "sources": ["official:none"],
                        "note": "测试用裸 OP"
                    }}],
    }
    movie_show, extra_paths = match.assemble_movie(movie_plan)
    try:
        match.validate_special_airdates(movie_show, extra_paths)
        raise AssertionError("应拒绝缺少 airdate 的 movie extras")
    except ValueError as exc:
        assert "airdate" in str(exc) and "电影 NCOP" in str(exc), str(exc)
    print("  [ok] Season 0 / movie extras airdate 护栏正确")

def test_artwork_explicit_source_only(td: Path):
    """显式 --no-hardlinks 时仍在源侧 materialize 图片(已有有效图则跳过)。"""
    src = td / "src"
    video = _touch(src / "ep01.mkv")
    thumb = _write_test_image(src / "ep01-thumb.jpg")
    poster = _write_test_image(src / "poster.jpg", color=(120, 80, 60))
    thumb_before = thumb.read_bytes()
    poster_before = poster.read_bytes()
    plan = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "tv", "output_dir": str(src),
        "show": {"title": "仅源侧", "sorttitle": "仅源侧 2024-01-01 仅源侧",
                 "premiered": "2024-01-01", "lockdata": True},
        "library_projection": {"hardlinks_enabled": False, "link_root": None},
        "episodes": [{"category": "normal", "season": 1, "episode": 1,
                      "title": "第一话", "video_path": str(video),
                      "plot_evidence": {
                          "bangumi_zh": "empty", "tmdb_zh": "empty",
                          "bangumi_ja": "empty", "tmdb_en": "empty",
                      }}],
        # 无 library_relpath：仅源侧模式允许
        "artwork": [
            {"scope": "episode", "kind": "episode_thumb",
             "source_path": str(thumb), "method": "tmdb",
             "url": "https://example.invalid/must-skip"},
            {"scope": "show", "kind": "poster",
             "source_path": str(poster), "method": "tmdb",
             "url": "https://example.invalid/must-skip"},
        ],
        "artwork_review": {
            "schema": "anime-scraper-artwork-review-v1",
            "status": "disabled",
            "multimodal_review_enabled": False,
            "selection_method": "deterministic_existing_pipeline",
            "preview": {"width": 320, "height": 480},
            "candidate_limit": 5,
            "source_manifest_sha256": "0" * 64,
            "groups": [],
        },
    }
    plan_file = td / "plan.json"
    plan_file.write_text(__import__("json").dumps(plan, ensure_ascii=False), encoding="utf-8")
    original_link_root = scrape.link_root
    try:
        def unexpected_link_root(**_kwargs):
            raise AssertionError("默认源侧模式不应解析或建立硬链接库")

        scrape.link_root = unexpected_link_root
        assert scrape.main(["--plan", str(plan_file), "--no-hardlinks"]) == 0
    finally:
        scrape.link_root = original_link_root
    assert thumb.read_bytes() == thumb_before
    assert poster.read_bytes() == poster_before
    assert (src / "tvshow.nfo").is_file()
    print("  [ok] --no-hardlinks 时源侧 artwork 实体化(skip 已有图、不触网)")

def test_images(td: Path):
    """images:tmdb_image_url 拼接 + verify_thumbs 覆盖率检查(纯离线部分)。"""
    import images

    assert images.tmdb_image_url("/abc.jpg") == "https://image.tmdb.org/t/p/original/abc.jpg"
    assert images.tmdb_image_url("abc.jpg").endswith("/abc.jpg")
    assert images.tmdb_image_url("https://x/y.jpg") == "https://x/y.jpg"
    assert images.tmdb_image_url("") == ""

    d = td / "lib" / "Season 01"
    d.mkdir(parents=True)
    (d / "剧 S01E01.mkv").write_bytes(b"v")
    _write_test_image(d / "剧 S01E01-thumb.jpg")
    (d / "剧 S01E02.mkv").write_bytes(b"v")          # 无 thumb → 应被抓出
    (d / "notes.txt").write_bytes(b"x")               # 非视频 → 忽略
    sub = td / "lib" / "Specials"
    sub.mkdir()
    (sub / "剧 S00E01.mkv").write_bytes(b"v")         # 无 thumb(递归时应被抓出)

    miss = images.verify_thumbs(d)
    assert [m.name for m in miss] == ["剧 S01E02.mkv"], miss
    miss_r = images.verify_thumbs(td / "lib", recursive=True)
    assert sorted(m.name for m in miss_r) == ["剧 S00E01.mkv", "剧 S01E02.mkv"], miss_r
    print("  [ok] images(tmdb_image_url/verify_thumbs 单层与递归)正确")


def test_image_download_recovery_and_workers(td: Path):
    """图片断流可 Range 恢复，且两个并发上限来自 artwork 配置。"""
    import threading
    import time

    config = {"artwork": {"tmdb_workers": 3, "ffmpeg_workers": 2}}
    assert images.artwork_worker_config(config) == {
        "tmdb_workers": 3, "ffmpeg_workers": 2,
    }
    for invalid in (
        {"artwork": {"tmdb_workers": 0, "ffmpeg_workers": 2}},
        {"artwork": {"tmdb_workers": True, "ffmpeg_workers": 2}},
        {"artwork": {"tmdb_workers": 3, "ffmpeg_workers": 9}},
    ):
        try:
            images.artwork_worker_config(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("图片并发配置越界或布尔值必须拒绝")

    payload_path = _write_test_image(td / "payload.jpg")
    payload = payload_path.read_bytes()
    destination = td / "downloaded.jpg"
    cut = max(1, len(payload) // 2)
    requests: list[str | None] = []

    class FakeResponse:
        def __init__(self, body: bytes, *, status: int, headers: dict[str, str], broken=False):
            self.body = body
            self.status = status
            self.headers = headers
            self.broken = broken
            self.read_once = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size: int):
            if self.broken and not self.read_once:
                self.read_once = True
                return self.body
            if self.broken:
                raise images.IncompleteRead(b"")
            if self.read_once:
                return b""
            self.read_once = True
            return self.body

        def getcode(self):
            return self.status

    original_urlopen = images.urlopen

    def fake_urlopen(request, timeout):
        assert timeout == 1
        range_header = request.headers.get("Range")
        requests.append(range_header)
        if range_header is None:
            return FakeResponse(
                payload[:cut], status=200,
                headers={"Content-Length": str(len(payload))}, broken=True,
            )
        assert range_header == f"bytes={cut}-"
        return FakeResponse(
            payload[cut:], status=206,
            headers={"Content-Range": f"bytes {cut}-{len(payload) - 1}/{len(payload)}"},
        )

    images.urlopen = fake_urlopen
    try:
        assert images.download_image(
            "https://image.test/recover.jpg", destination,
            timeout=1, max_attempts=2, backoff=0, throttle=0,
        )
    finally:
        images.urlopen = original_urlopen
    assert destination.read_bytes() == payload
    assert requests == [None, f"bytes={cut}-"], requests
    assert not list(td.glob("*.part"))
    assert images.download_image(
        "https://image.test/recover.jpg", destination,
        timeout=1, throttle=0, return_size=True,
    ) == (2, 2)
    requests.clear()
    images.urlopen = fake_urlopen
    try:
        downloaded, downloaded_size = images.download_bytes(
            "https://image.test/bytes.jpg", timeout=1,
            return_size=True,
        )
    finally:
        images.urlopen = original_urlopen
    assert downloaded == payload
    assert downloaded_size == (2, 2)

    original_load_config = images.load_config
    original_materialize = images.materialize_artwork
    active = {"tmdb": 0, "frame": 0}
    maximum = {"tmdb": 0, "frame": 0}
    lock = threading.Lock()

    def fake_materialize(item, *, skip_existing=True):
        method = item["method"]
        with lock:
            active[method] += 1
            maximum[method] = max(maximum[method], active[method])
        time.sleep(0.02)
        with lock:
            active[method] -= 1
        return Path(item["source_path"])

    images.load_config = lambda: config
    images.materialize_artwork = fake_materialize
    try:
        items = [
            {"source_path": str(td / f"tmdb-{index}.jpg"), "method": "tmdb",
             "url": f"https://image.test/{index}.jpg"}
            for index in range(8)
        ] + [
            {"source_path": str(td / f"frame-{index}.jpg"), "method": "frame",
             "fallback_video_path": str(td / f"video-{index}.mkv")}
            for index in range(5)
        ]
        result = images.materialize_artwork_batch(items, progress=False)
    finally:
        images.load_config = original_load_config
        images.materialize_artwork = original_materialize
    assert len(result) == len(items)
    assert maximum == {"tmdb": 3, "frame": 2}, maximum
    print("  [ok] 图片 Range 断点恢复、配置并发上限和任务去重正确")

def test_bootstrap_runtime_location():
    assert bootstrap.MIN_PYTHON == (3, 10)
    assert bootstrap.python_supported((3, 10))
    assert not bootstrap.python_supported((3, 9))
    assert bootstrap.runtime_root() == ROOT / ".runtime" / "venvs"
    assert bootstrap.pip_cache_root() == ROOT / ".runtime" / "pip-cache"
    assert bootstrap.VENV_DIR.parent == bootstrap.runtime_root()
    assert ROOT in bootstrap.VENV_DIR.parents
    assert bootstrap.environment_key().startswith(f"py{sys.version_info.major}{sys.version_info.minor}-")
    print("  [ok] bootstrap 使用按 Python/requirements 隔离的 skill 本地环境")

def test_bootstrap_run_target_guard(td: Path):
    td.mkdir(parents=True, exist_ok=True)
    identify_args = bootstrap.validated_run_args(["scripts/identify.py", "--help"])
    assert Path(identify_args[0]) == (ROOT / "scripts" / "identify.py").resolve()
    assert identify_args[1:] == ["--help"]

    absolute_args = bootstrap.validated_run_args([
        str(ROOT / "tests" / "smoke_test.py")
    ])
    assert Path(absolute_args[0]) == (ROOT / "tests" / "smoke_test.py").resolve()

    outside = td / "outside.py"
    outside.write_text("print('outside')\n", encoding="utf-8")
    for denied in (["-c", "print('x')"], [str(outside)], ["../outside.py"]):
        try:
            bootstrap.validated_run_args(denied)
            raise AssertionError(f"必须拒绝非 skill 脚本入口: {denied}")
        except ValueError:
            pass
    print("  [ok] bootstrap --run 仅限制代码入口，不改变脚本参数")

def test_cli_help_is_side_effect_free(td: Path):
    scripts = (
        "anidb_episodes.py",
        "anidb_titles.py",
        "artwork_review.py",
        "images.py",
        "probe_duration.py",
        "tmdb.py",
    )
    for script in scripts:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), "--help"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, (script, result.stdout, result.stderr)
        assert "usage:" in result.stdout.lower(), (script, result.stdout)
    print("  [ok] 手工 CLI --help 不读配置、不联网、不扫描路径")

def test_step_zero_machine_marker(td: Path):
    """Step 0 完成标记仅对写入它的本机有效。"""
    original_root = bootstrap.ROOT
    original_fingerprint = bootstrap.machine_fingerprint
    try:
        bootstrap.ROOT = td / "skill"
        assert not bootstrap.step_zero_completed()
        marker = bootstrap.mark_step_zero_complete()
        assert marker == bootstrap.step_zero_marker_path()
        assert marker.is_file()
        assert bootstrap.step_zero_completed()
        payload = json.loads(marker.read_text(encoding="utf-8"))
        assert payload["schema_version"] == bootstrap.STEP_ZERO_SCHEMA_VERSION
        assert "machine_fingerprint" in payload

        bootstrap.machine_fingerprint = lambda: "different-machine"
        assert not bootstrap.step_zero_completed()
    finally:
        bootstrap.ROOT = original_root
        bootstrap.machine_fingerprint = original_fingerprint
    print("  [ok] Step 0 本机完成标记正确隔离")

def test_skill_root_config(td: Path):
    """配置直接使用 skill 根目录 config.json。"""
    skill_root = td / "skill"
    config_file = skill_root / "config.json"
    write_test_config(config_file)
    with config_root(skill_root):
        assert _common.config_path() == skill_root / "config.json"
        assert _common.load_config()["paths"]["source_root"] == ""
        assert _common.multimodal_artwork_review_enabled() is False
        assert _common.artwork_cache_enabled() is False
        configured = _common.load_config()
        configured["artwork"]["multimodal_review"] = True
        config_file.write_text(json.dumps(configured), encoding="utf-8")
        assert _common.multimodal_artwork_review_enabled() is True
        configured["artwork"]["multimodal_review"] = "false"
        config_file.write_text(json.dumps(configured), encoding="utf-8")
        try:
            _common.multimodal_artwork_review_enabled()
            raise AssertionError("多模态开关必须拒绝字符串布尔值")
        except ValueError as exc:
            assert "必须是 true 或 false" in str(exc)
        configured["artwork"]["multimodal_review"] = False
        configured["artwork"]["artwork_cache"] = True
        config_file.write_text(json.dumps(configured), encoding="utf-8")
        assert _common.artwork_cache_enabled() is True
        configured["artwork"]["artwork_cache"] = "false"
        config_file.write_text(json.dumps(configured), encoding="utf-8")
        try:
            _common.artwork_cache_enabled()
            raise AssertionError("原图缓存开关必须拒绝字符串布尔值")
        except ValueError as exc:
            assert "必须是 true 或 false" in str(exc)
        configured["artwork"]["artwork_cache"] = False
        config_file.write_text(json.dumps(configured), encoding="utf-8")

        portable_root = td / "portable-skill"
        with config_root(portable_root):
            default_cache = _common.cache_dir({"cache_dir": ""}, "bangumi")
            relative_cache = _common.cache_dir({"cache_dir": "api-cache"}, "tmdb")
            absolute_base = td / "absolute-cache"
            absolute_cache = _common.cache_dir(
                {"cache_dir": str(absolute_base)}, "anidb"
            )
            assert default_cache == portable_root / "cache" / "bangumi"
            assert relative_cache == portable_root / "api-cache" / "tmdb"
            assert absolute_cache == absolute_base / "anidb"
    print("  [ok] 配置直接使用 skill 根目录 config.json")

def test_cached_replace_cli_shortcut(td: Path):
    """快捷 CLI 直接从 plan/cache 替换，不需要 change-set JSON。"""
    source = td / "source" / "poster.jpg"
    source.parent.mkdir(parents=True)
    _write_test_image(source, color=(12, 34, 56))
    library_root = td / "library"
    project = library_root / "Demo (2020)"
    (project / "Season 01").mkdir(parents=True)
    root_destination = project / "poster.jpg"
    season_destination = project / "Season 01" / "poster.jpg"
    os.link(source, root_destination)
    os.link(source, season_destination)

    cache_dir = td / "cache" / "demo"
    cache_dir.mkdir(parents=True)
    cached = cache_dir / "Demo - s1 - G01-C01.jpg"
    _write_test_image(cached, color=(200, 30, 40))
    cache_manifest = {
        "schema": artwork_cache.SCHEMA,
        "status": "completed",
        "groups": [{
            "group_id": "season-01",
            "candidates": [{
                "candidate_id": "G01-C01",
                "url": "https://image.tmdb.org/t/p/original/shortcut.jpg",
                "cache_path": str(cached),
                "width": 100,
                "height": 150,
            }],
        }],
    }
    (cache_dir / artwork_cache.MANIFEST_NAME).write_text(
        json.dumps(cache_manifest), encoding="utf-8"
    )
    plan_path = td / "plan.json"
    plan_path.write_text(json.dumps({
        "plan_schema": "anime-scraper-plan",
        "type": "tv",
        "library_projection": {
            "hardlinks_enabled": True,
            "link_root": str(library_root),
        },
        "show": {"title": "Demo", "premiered": "2020-01-01"},
        "artwork_review": {"original_cache_dir": str(cache_dir)},
        "artwork": [
            {"kind": "poster", "source_path": str(source),
             "library_relpath": "poster.jpg", "method": "tmdb",
             "url": "https://image.tmdb.org/t/p/original/old.jpg"},
            {"kind": "poster", "source_path": str(source),
             "library_relpath": "Season 01/poster.jpg", "method": "tmdb",
             "url": "https://image.tmdb.org/t/p/original/old.jpg"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    report_path = td / "shortcut.report.json"
    result = update_artwork.main([
        "--cached-replace", "--plan", str(plan_path),
        "--candidate-id", "G01C01", "--target", "main-poster",
        "--report", str(report_path),
    ])
    assert result == 0 and report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["mode"] == "cached-replace"
    assert report["candidate_id"] == "G01-C01"
    current_cached = cached.with_name(f"{cached.stem} - CURRENT{cached.suffix}")
    assert current_cached.is_file()
    assert report["cache_path"] == str(current_cached.resolve())
    assert not (td / "change-set.json").exists()
    assert images.verify_hardlink(source, root_destination)
    assert images.verify_hardlink(source, season_destination)
    updated_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert all(
        entry.get("candidate_id") == "G01-C01"
        for entry in updated_plan["artwork"]
    )
    print("  [ok] 缓存海报快捷 CLI 不落地 change-set JSON 且完成目标级替换")


def _write_direct_cache_candidate(cache_dir: Path, candidate_id: str, *, color):
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"candidate-{candidate_id}.jpg"
    _write_test_image(cached, color=color)
    manifest = {
        "schema": artwork_cache.SCHEMA,
        "status": "completed",
        "groups": [{
            "group_id": "season-01",
            "candidates": [{
                "candidate_id": candidate_id,
                "url": f"https://image.tmdb.org/t/p/original/{candidate_id}.jpg",
                "cache_path": str(cached),
                "width": 2,
                "height": 2,
            }],
        }],
    }
    manifest_path = cache_dir / artwork_cache.MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return cached, manifest_path.read_bytes()


def test_cached_replace_direct_main(td: Path):
    """无 plan 主海报快捷模式只处理显式 poster 白名单。"""
    source_dir = td / "source"
    source = _write_test_image(source_dir / "poster.jpg", color=(12, 34, 56))
    library_dir = td / "library" / "Demo (2020)"
    season_dir = library_dir / "Season 01"
    season_dir.mkdir(parents=True)
    root_destination = library_dir / "poster.jpg"
    season_destination = season_dir / "poster.jpg"
    os.link(source, root_destination)
    season_destination.write_bytes(source.read_bytes())
    cached, manifest_before = _write_direct_cache_candidate(
        td / "cache", "G01-C02", color=(200, 30, 40)
    )
    report_path = td / "direct-main.report.json"

    assert update_artwork.main([
        "--cached-replace", "--source-dir", str(source_dir),
        "--library-dir", str(library_dir), "--original-cache-dir",
        str(cached.parent), "--candidate-id", "G01C02",
        "--target", "main-poster", "--report", str(report_path),
    ]) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["mode"] == "cached-replace-direct"
    assert report["plan_synced"] is False
    assert report["downloads"] == 0 and report["ffmpeg"] == 0
    assert images.verify_hardlink(source, root_destination)
    assert images.verify_hardlink(source, season_destination)
    renamed = cached.with_name(f"{cached.stem} - CURRENT{cached.suffix}")
    assert renamed.is_file() and not cached.exists()
    assert report["cache_path"] == str(renamed.resolve())
    assert report["marker_synced"] is True
    assert report["marker_groups"] == {"season-01": "G01-C02"}
    manifest_after = json.loads(
        (td / "cache" / artwork_cache.MANIFEST_NAME).read_text(encoding="utf-8")
    )
    group = manifest_after["groups"][0]
    assert group["group_id"] == "season-01"
    assert group["current_candidate_id"] == "G01-C02"
    assert group["candidates"][0]["cache_path"] == str(renamed.resolve())
    print("  [ok] 无 plan 主海报快捷替换只处理根/现有季海报白名单并同步目标组标记")


def test_cached_replace_direct_specials(td: Path):
    """无 plan Specials 快捷模式只处理 specials-poster 与 Specials/poster。"""
    source_dir = td / "source"
    source = _write_test_image(
        source_dir / "specials-poster.jpg", color=(12, 34, 56)
    )
    library_dir = td / "library" / "Demo (2020)"
    specials_dir = library_dir / "Specials"
    specials_dir.mkdir(parents=True)
    destination = specials_dir / "poster.jpg"
    os.link(source, destination)
    cached, manifest_before = _write_direct_cache_candidate(
        td / "cache", "G01-C03", color=(40, 180, 90)
    )
    report_path = td / "direct-specials.report.json"

    assert update_artwork.main([
        "--cached-replace", "--source-dir", str(source_dir),
        "--library-dir", str(library_dir), "--original-cache-dir", str(cached.parent),
        "--candidate-id", "G01-C03", "--target", "specials-poster",
        "--report", str(report_path),
    ]) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["mode"] == "cached-replace-direct"
    assert report["target"] == "specials-poster"
    assert report["plan_synced"] is False
    assert report["downloads"] == 0 and report["ffmpeg"] == 0
    assert images.verify_hardlink(source, destination)
    assert report["marker_synced"] is False
    assert "specials" in report["marker_note"]
    assert (td / "cache" / artwork_cache.MANIFEST_NAME).read_bytes() == manifest_before
    print("  [ok] 无 plan Specials 快捷替换只处理 Specials 海报白名单")


def test_cached_replace_direct_marker_pools(td: Path):
    """快捷模式按目标组同步 CURRENT；跨池安装通过共享文件别名落到 specials 组。"""
    cache_dir = td / "cache"
    c01 = _write_test_image(cache_dir / "c01 - CURRENT.jpg", color=(10, 20, 30))
    c17 = _write_test_image(cache_dir / "c17 - CURRENT.jpg", color=(30, 40, 50))
    c21 = _write_test_image(cache_dir / "c21.jpg", color=(50, 60, 70))
    cache_manifest = {
        "schema": artwork_cache.SCHEMA,
        "status": "completed",
        "groups": [
            {
                "group_id": "tv-show",
                "current_candidate_id": "G01-C01",
                "candidates": [
                    {"candidate_id": "G01-C01", "url": "https://image.tmdb.org/t/p/original/c01.jpg",
                     "cache_path": str(c01), "width": 2, "height": 2},
                    {"candidate_id": "G01-C21", "url": "https://image.tmdb.org/t/p/original/c21.jpg",
                     "cache_path": str(c21), "width": 2, "height": 2},
                ],
            },
            {
                "group_id": "specials",
                "current_candidate_id": "G03-C17",
                "candidates": [
                    {"candidate_id": "G03-C17", "url": "https://image.tmdb.org/t/p/original/c17.jpg",
                     "cache_path": str(c17), "width": 2, "height": 2},
                    {"candidate_id": "G03-C21", "url": "https://image.tmdb.org/t/p/original/c21.jpg",
                     "cache_path": str(c21), "width": 2, "height": 2},
                ],
            },
        ],
    }
    (cache_dir / artwork_cache.MANIFEST_NAME).write_text(
        json.dumps(cache_manifest), encoding="utf-8"
    )

    source_dir = td / "source"
    source = _write_test_image(source_dir / "poster.jpg", color=(12, 34, 56))
    specials_source = _write_test_image(
        source_dir / "specials-poster.jpg", color=(12, 34, 56)
    )
    library_dir = td / "library" / "Demo (2020)"
    (library_dir / "Specials").mkdir(parents=True)
    (library_dir / "tvshow.nfo").write_text("<tvshow/>", encoding="utf-8")
    root_destination = library_dir / "poster.jpg"
    specials_destination = library_dir / "Specials" / "poster.jpg"
    os.link(source, root_destination)
    os.link(specials_source, specials_destination)

    main_report_path = td / "direct-pools-main.report.json"
    assert update_artwork.main([
        "--cached-replace", "--source-dir", str(source_dir),
        "--library-dir", str(library_dir), "--original-cache-dir", str(cache_dir),
        "--candidate-id", "G01C21", "--target", "main-poster",
        "--report", str(main_report_path),
    ]) == 0
    main_report = json.loads(main_report_path.read_text(encoding="utf-8"))
    assert main_report["marker_synced"] is True
    assert main_report["marker_groups"] == {"tv-show": "G01-C21"}
    c21_current = cache_dir / "c21 - CURRENT.jpg"
    assert c21_current.is_file() and not c21.exists()
    assert not (cache_dir / "c01 - CURRENT.jpg").exists()
    assert (cache_dir / "c01.jpg").is_file()
    assert main_report["cache_path"] == str(c21_current.resolve())

    specials_report_path = td / "direct-pools-specials.report.json"
    assert update_artwork.main([
        "--cached-replace", "--source-dir", str(source_dir),
        "--library-dir", str(library_dir), "--original-cache-dir", str(cache_dir),
        "--candidate-id", "G01C21", "--target", "specials-poster",
        "--report", str(specials_report_path),
    ]) == 0
    specials_report = json.loads(specials_report_path.read_text(encoding="utf-8"))
    assert specials_report["marker_synced"] is True
    assert specials_report["marker_groups"] == {"specials": "G03-C21"}
    manifest_after = json.loads(
        (cache_dir / artwork_cache.MANIFEST_NAME).read_text(encoding="utf-8")
    )
    currents = {
        group["group_id"]: group["current_candidate_id"]
        for group in manifest_after["groups"]
    }
    assert currents == {"tv-show": "G01-C21", "specials": "G03-C21"}
    assert not (cache_dir / "c17 - CURRENT.jpg").exists()
    print("  [ok] 无 plan 快捷替换按目标组同步 CURRENT 并支持跨池共享别名")


def test_cached_replace_noop_and_review_sync(td: Path):
    """同候选替换幂等不崩溃；替换后 artwork_review 候选池与 selection 同步。"""
    source = td / "source" / "poster.jpg"
    source.parent.mkdir(parents=True)
    _write_test_image(source, color=(12, 34, 56))
    library_root = td / "library"
    project = library_root / "Demo (2020)"
    (project / "Season 01").mkdir(parents=True)
    root_destination = project / "poster.jpg"
    season_destination = project / "Season 01" / "poster.jpg"
    os.link(source, root_destination)
    os.link(source, season_destination)

    cache_dir = td / "cache" / "demo"
    cache_dir.mkdir(parents=True)
    old_cached = cache_dir / "Demo - s1 - G01-C01.jpg"
    new_cached = cache_dir / "Demo - s1 - G01-C02.jpg"
    _write_test_image(old_cached, color=(10, 10, 10))
    _write_test_image(new_cached, color=(200, 30, 40))
    old_url = "https://image.tmdb.org/t/p/original/old.jpg"
    new_url = "https://image.tmdb.org/t/p/original/new.jpg"
    (cache_dir / artwork_cache.MANIFEST_NAME).write_text(json.dumps({
        "schema": artwork_cache.SCHEMA,
        "status": "completed",
        "groups": [{
            "group_id": "s1-poster",
            "candidates": [
                {"candidate_id": "G01-C01", "url": old_url,
                 "cache_path": str(old_cached), "width": 100, "height": 150},
                {"candidate_id": "G01-C02", "url": new_url,
                 "cache_path": str(new_cached), "width": 2000, "height": 3000},
            ],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    plan_path = td / "plan.json"
    poster_entry = {"kind": "poster", "source_path": str(source),
                    "library_relpath": "poster.jpg", "method": "tmdb",
                    "url": old_url, "candidate_id": "G01-C01"}
    plan_path.write_text(json.dumps({
        "plan_schema": "anime-scraper-plan",
        "type": "tv",
        "library_projection": {"hardlinks_enabled": True,
                               "link_root": str(library_root)},
        "show": {"title": "Demo", "premiered": "2020-01-01"},
        "artwork_review": {
            "original_cache_dir": str(cache_dir),
            "groups": [{"group_id": "s1-poster", "candidates": [
                {"candidate_id": "G01-C01", "url": old_url, "width": 100,
                 "height": 150, "language": "ja", "resolution_class": "low"},
            ]}],
            "selections": [{"group_id": "s1-poster", "candidate_id": "G01-C01",
                            "confidence": "high", "reason": "deterministic",
                            "flags": [], "decision_factors": None}],
        },
        "artwork": [poster_entry,
                    {"kind": "poster", "source_path": str(source),
                     "library_relpath": "Season 01/poster.jpg", "method": "tmdb",
                     "url": old_url, "candidate_id": "G01-C01"}],
    }, ensure_ascii=False), encoding="utf-8")

    # 1) 同候选 no-op：不崩溃，updated=0/resumed=1，review 保持不变
    assert update_artwork.main([
        "--cached-replace", "--plan", str(plan_path),
        "--candidate-id", "G01C01", "--target", "main-poster",
    ]) == 0
    review = json.loads(plan_path.read_text(encoding="utf-8"))["artwork_review"]
    assert review["selections"][0]["candidate_id"] == "G01-C01"
    assert review["groups"][0]["candidates"][0]["candidate_id"] == "G01-C01"

    # 2) 替换为新候选：artwork 与 review 同步，新候选位于 candidates[0]
    assert update_artwork.main([
        "--cached-replace", "--plan", str(plan_path),
        "--candidate-id", "G01C02", "--target", "main-poster",
    ]) == 0
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    root_entry = next(item for item in plan["artwork"]
                      if item.get("library_relpath") == "poster.jpg")
    assert root_entry["candidate_id"] == "G01-C02"
    assert root_entry["url"] == new_url
    review = plan["artwork_review"]
    group = next(g for g in review["groups"] if g["group_id"] == "s1-poster")
    assert group["candidates"][0]["candidate_id"] == "G01-C02"
    assert group["candidates"][0]["url"] == new_url
    assert group["candidates"][0]["resolution_class"] == "low"  # 测试图为极小尺寸，等级按解码结果计算
    selection = next(s for s in review["selections"]
                     if s["group_id"] == "s1-poster")
    assert selection["candidate_id"] == "G01-C02"
    assert images.verify_hardlink(source, root_destination)
    assert images.verify_hardlink(source, season_destination)
    print("  [ok] 同候选替换幂等且 artwork_review 候选池自动同步")


def test_plan_scaffold(td: Path):
    """plan_scaffold:确定性字段进草稿,语义字段进 agent_todo,scrape 拒绝草稿标记。"""
    snapshot = {
        "bangumi": {"subjects": {"253": {
            "subject": {
                "name": "テスト", "name_cn": "测试动画", "summary": "剧情简介",
                "date": "2020-01-05", "infobox": {"动画制作": "某工作室"},
                "rating": {"score": 7.84, "total": 120},
            },
            "episodes": [
                {"type_id": 0, "ep": 1, "sort": 1, "name": "第一話", "name_cn": "第一话",
                 "airdate": "2020-01-05", "desc": "第一话简介", "duration": "00:24:00"},
                {"type_id": 0, "ep": 2, "sort": 2, "name": "第二話", "name_cn": "",
                 "airdate": "2020-01-12", "desc": "", "duration": "00:23:40"},
            ],
            "characters": [], "persons": [], "themes": {},
        }}},
        "anidb": {"episodes": {}},
        "tmdb": {"tv": {"5": {"seasons": {"1": {"episodes": [
            {"episode_number": 2, "overview": "TMDB 中文回退简介"},
        ]}}}}, "movie": {}},
    }
    manifest = {
        "root": str(td / "src"),
        "files": [
            {"rel_path": "EP01.mkv", "stem": "EP01", "subdir_special_hint": False,
             "hint": {"episode_number": 1, "is_special": False, "episode_type": "normal"}},
            {"rel_path": "EP02.mkv", "stem": "EP02", "subdir_special_hint": False,
             "hint": {"episode_number": 2, "is_special": False, "episode_type": "normal"}},
            {"rel_path": "SPs/NCOP.mkv", "stem": "NCOP", "subdir_special_hint": True, "duration": 90,
             "hint": {"episode_number": None, "is_special": True, "episode_type": "credit"}},
        ],
    }
    args = plan_scaffold._parse_args([
        "--snapshot", "x", "--manifest", "x", "--output", "x",
        "--bgm-id", "253", "--anidb-aid", "9999",
        "--tmdb-tv-id", "5", "--tmdb-main-season", "1",
    ])
    plan = plan_scaffold.scaffold_tv(snapshot, manifest, args, load_test_config())

    show = plan["show"]
    assert show["title"] == "测试动画" and show["sorttitle"] == ""
    assert show["bgm_id"] == 253 and show["anidb_aid"] == 9999
    assert show["rating"] == 7.8 and show["premiered"] == "2020-01-05"
    assert show["studio"] == "某工作室"
    assert show["staff_status"] == "empty"          # 空 persons → 显式空 + 审计
    assert show["staff_audit"]["mappable_crew_count"] == 0

    eps = plan["episodes"]
    assert [e["episode"] for e in eps] == [1, 2]
    assert eps[0]["plot"] == "第一话简介" and eps[0]["plot_source"] == "bangumi_desc"
    assert eps[0]["runtime"] == 24 and eps[0]["tmdb_match_status"] == "unknown"
    assert eps[1]["plot"] == "TMDB 中文回退简介" and eps[1]["plot_source"] == "tmdb_overview"
    assert eps[1]["title"] == "第二話"              # name_cn 空 → 回退原名
    assert eps[0]["video_path"].endswith("EP01.mkv")

    # 每个已匹配视频有同 stem 的 frame thumb 条目;library_relpath 留待 Agent。
    assert len(plan["artwork"]) == 2
    assert plan["artwork"][0]["source_path"].endswith("EP01-thumb.jpg")
    assert plan["artwork"][0]["method"] == "frame"
    assert plan["artwork"][0]["library_relpath"] == ""

    scaffold = plan["scaffold"]
    specials = scaffold["worksheet"]["special_files"]
    assert len(specials) == 1 and specials[0]["stem"] == "NCOP"
    assert any("sorttitle" in item for item in scaffold["agent_todo"])
    assert plan["library_projection"].keys() == {"hardlinks_enabled", "link_root"}

    # scrape 在任何写入前拒绝仍含 scaffold 标记的 plan;删除标记后放行 schema 检查。
    try:
        scrape._require_plan_schema(plan)
        raise AssertionError("scrape 未拒绝含 scaffold 标记的 plan")
    except ValueError as exc:
        assert "scaffold" in str(exc)
    finished = {k: v for k, v in plan.items() if k != "scaffold"}
    scrape._require_plan_schema(finished)
    print("  [ok] plan_scaffold 草稿生成与 scrape 草稿护栏")


def _request_poster(file_path: str, language: str, votes: int,
                    width: int, height: int) -> dict:
    return {
        "kind": "poster", "file_path": file_path,
        "url": f"https://image.tmdb.org/t/p/original{file_path}",
        "language": language, "vote_count": votes, "vote_average": 5.0,
        "width": width, "height": height,
    }


def _request_pool(count: int, prefix: str, language: str = "ja") -> list[dict]:
    return [
        _request_poster(f"/{prefix}{index}.jpg", language, count - index,
                        1000 + index, 1500 + index)
        for index in range(count)
    ]


def test_artwork_build_request(td: Path):
    """--build-request:从快照确定性组装请求 JSON,零手工誊写,离线可测。"""
    season_one = _request_pool(8, "s1")
    season_zero = _request_pool(4, "z")
    single_season = _request_pool(3, "a", "zh")
    series_pool = _request_pool(3, "b", "en")
    movie_pool = _request_pool(5, "m")
    snapshot = {
        "tmdb": {
            "tv": {
                "5": {
                    "detail": {"seasons": [
                        {"season_number": 1}, {"season_number": 2}]},
                    "seasons": {
                        "1": {"images": {"posters": season_one}},
                        "0": {"images": {"posters": season_zero}},
                    },
                },
                "6": {
                    "detail": {"seasons": [{"season_number": 1}]},
                    "seasons": {"1": {"images": {"posters": single_season}}},
                    "images": {"posters": series_pool},
                },
            },
            "movie": {"7": {"images": {"posters": movie_pool}}},
        }
    }

    def fake_specials(tv_id, main_season):
        if tv_id == 5:
            return {"selection": "season_zero",
                    "candidate": dict(season_zero[0])}
        return {"selection": "none", "candidate": None}

    payload, notices = artwork_review.build_request(
        snapshot, series_name="测试系列",
        season_groups=[
            {"tv_id": 5, "season": 1, "group_id": "season-01", "label": "S1"},
            {"tv_id": 6, "season": 1, "group_id": "season-01b", "label": "S1"},
        ],
        movie_groups=[{"movie_id": 7, "group_id": "movie-00", "label": "剧场版"}],
        specials_groups=[
            {"tv_id": 5, "main_season": 1, "group_id": "season-00", "label": "Specials"},
            {"tv_id": 6, "main_season": 1, "group_id": "skipme", "label": "无候选"},
        ],
        specials_runner=fake_specials,
    )
    groups = {group["group_id"]: group for group in payload["groups"]}
    assert payload["series_name"] == "测试系列"
    assert set(groups) == {"season-01", "season-01b", "movie-00", "season-00"}
    assert any("跳过该组" in notice for notice in notices)

    multi = groups["season-01"]
    assert multi["cache_candidates"] == season_one          # 多季作品不合并系列池
    assert multi["candidates"] == tmdb.poster_review_candidates(season_one, 5)
    assert multi["deterministic_selection"]["file_path"] == \
        tmdb.poster_review_candidates(season_one, 1)[0]["file_path"]
    assert multi["work_name"] == "S1"

    merged = groups["season-01b"]
    assert merged["cache_candidates"] == single_season + series_pool  # 单正式季合并

    specials = groups["season-00"]
    specials_paths = {item["file_path"] for item in specials["cache_candidates"]}
    assert season_zero[0]["file_path"] in specials_paths
    assert specials["deterministic_selection"]["file_path"] == season_zero[0]["file_path"]

    # 三段选择结果必须占一个识图席位:Agent 看不到它就既无法确认也无法否决。
    # season_zero 的候选来自 Season 0 池,票数天然低于主池;若识图池混入主池,
    # 高票主海报会占满 5 个席位把它挤掉(artwork-library.md §6)。
    specials_seats = [item["file_path"] for item in specials["candidates"]]
    assert season_zero[0]["file_path"] in specials_seats, specials_seats
    assert len(specials["candidates"]) <= artwork_review.CANDIDATE_LIMIT
    # 缓存池仍覆盖主池,原图缓存不因识图池收窄而漏图。
    assert specials_paths >= {item["file_path"] for item in season_zero}
    # 识图席位 ⊆ 缓存池(build_review 的硬性前置)。
    assert set(specials_seats) <= specials_paths

    assert groups["movie-00"]["cache_candidates"] == movie_pool

    # 请求 JSON 必须能直接进入 build_review 的关闭多模态路径(全程离线)。
    review = artwork_review.build_review(
        payload, td / "review-out", multimodal_enabled=False,
        preview_cache_dir=None, original_cache_root=None)
    assert review["status"] == "disabled"
    assert len(review["groups"]) == 4

    try:
        artwork_review.build_request(
            {"tmdb": {"tv": {"5": {"detail": {"seasons": [{"season_number": 1}]},
                                  "seasons": {"1": {"images": {"posters": []}}}}}}},
            series_name="x",
            season_groups=[{"tv_id": 5, "season": 1,
                            "group_id": "g", "label": "g"}],
            movie_groups=[], specials_groups=[],
            specials_runner=fake_specials)
        raise AssertionError("空海报池必须报错")
    except ValueError as exc:
        assert "--tmdb-season-images" in str(exc)

    spec = artwork_review._parse_group_spec(
        "95479:1:season-01=咒术回战 S1", ["tv_id", "season"], "T")
    assert spec == {"tv_id": 95479, "season": 1,
                    "group_id": "season-01", "label": "咒术回战 S1"}
    print("  [ok] artwork_review --build-request 确定性组装请求 JSON")


def test_plan_scaffold_apply_todo(td: Path):
    """--apply-todo:answers sidecar 合并语义字段,S00E/thumb/relpath 全部脚本化。"""
    snapshot = {
        "bangumi": {"subjects": {"253": {
            "subject": {
                "name": "テスト", "name_cn": "测试动画", "summary": "剧情简介",
                "date": "2020-01-05", "infobox": {"动画制作": "某工作室"},
                "rating": {"score": 7.84, "total": 120},
            },
            "episodes": [
                {"type_id": 0, "ep": 1, "sort": 1, "name": "第一話", "name_cn": "第一话",
                 "airdate": "2020-01-05", "desc": "第一话简介", "duration": "00:24:00"},
            ],
            "characters": [], "persons": [], "themes": {},
        }}},
        "anidb": {"episodes": {}},
        "tmdb": {"tv": {}, "movie": {}},
    }
    source_root = td / "src"
    manifest = {
        "root": str(source_root),
        "files": [
            {"rel_path": "EP01.mkv", "stem": "EP01", "subdir_special_hint": False,
             "hint": {"episode_number": 1, "is_special": False, "episode_type": "normal"}},
            {"rel_path": "SPs/NCOP.mkv", "stem": "NCOP", "subdir_special_hint": True, "duration": 90,
             "hint": {"episode_number": None, "is_special": True, "episode_type": "credit"}},
        ],
    }
    args = plan_scaffold._parse_args([
        "--snapshot", "x", "--manifest", "x", "--output", "x", "--bgm-id", "253",
    ])
    draft = plan_scaffold.scaffold_tv(snapshot, manifest, args, load_test_config())
    answers = {
        "apply_schema": "anime-scraper-todo-apply-v1",
        "output_dir": str(source_root),
        "sorttitle": "测试动画 2020-01-05 测试动画",
        "title": "测试动画",
        "specials": [{
            "rel_path": "SPs/NCOP.mkv", "episode": 1, "category": "credit",
            "title": "OP1 - 测试之歌", "airdate": "2020-01-05",
            "anidb_epno": "C1", "anidb_type": "credit",
            "special_order": {"priority": 20, "series_key": "OP",
                              "series_order": 1, "item_order": 1, "source_index": 0},
            "song_evidence": {"status": "resolved",
                              "sources": ["official:example.com"], "note": "官网对应"},
            "thumb": {"method": "frame"},
        }],
        "posters": {
            "main_poster": {"method": "tmdb",
                            "url": "https://image.tmdb.org/t/p/original/p.jpg"},
            "specials_poster": {"method": "tmdb",
                                "url": "https://image.tmdb.org/t/p/original/sp.jpg",
                                "specials_selection": "season_zero"},
        },
        "artwork_review": {"schema": "anime-scraper-artwork-review-v1",
                           "status": "disabled"},
    }
    final, notes = plan_scaffold.apply_todo(
        json.loads(json.dumps(draft)), manifest, answers)
    assert "scaffold" not in final and final["output_dir"] == str(source_root)
    assert final["show"]["sorttitle"].startswith("测试动画 ")
    scrape._require_plan_schema(final)

    episodes = {(ep["season"], ep["episode"]): ep for ep in final["episodes"]}
    special = episodes[(0, 1)]
    assert special["video_path"] == str(source_root / "SPs" / "NCOP.mkv")
    assert special["tmdb_match_status"] == "unknown"
    assert special["song_evidence"]["status"] == "resolved"

    artwork = {item["library_relpath"]: item for item in final["artwork"]}
    normal_thumb = artwork["Season 01/测试动画 S01E01-thumb.jpg"]
    assert normal_thumb["source_path"].endswith("EP01-thumb.jpg")
    special_thumb = artwork["Specials/测试动画 S00E01-thumb.jpg"]
    assert special_thumb["method"] == "frame"
    assert special_thumb["fallback_video_path"] == special["video_path"]
    poster = artwork["poster.jpg"]
    assert poster["source_path"] == str(source_root / "poster.jpg")
    assert "Season 01/poster.jpg" in artwork

    # kind 是 plan 契约字段,不等于源侧文件名:Specials 海报的源文件叫
    # specials-poster.jpg,kind 仍必须是 poster,否则识图护栏
    # (_validate_artwork_review) 与 --cached-replace 都按 kind=="poster" 取件,
    # 会静默漏掉这张图。
    specials_poster = artwork["Specials/poster.jpg"]
    assert specials_poster["kind"] == "poster", specials_poster
    assert specials_poster["source_path"] == str(source_root / "specials-poster.jpg")
    assert specials_poster["specials_selection"] == "season_zero"
    assert {item["kind"] for item in final["artwork"]
            if item["library_relpath"].endswith("poster.jpg")} == {"poster"}
    # 两张海报都必须进入识图护栏视野。
    reviewed = [item for item in final["artwork"]
                if str(item.get("kind") or "").lower() == "poster"
                and item.get("method") == "tmdb"]
    assert len(reviewed) == 3, reviewed  # 根 + Season 01 + Specials

    assert final["artwork_review"]["status"] == "disabled"
    assert any("S00E01" in note for note in notes)

    # 未处理的 worksheet 文件、credit 缺 song_evidence 必须拒绝。
    broken = {key: value for key, value in answers.items() if key != "specials"}
    try:
        plan_scaffold.apply_todo(json.loads(json.dumps(draft)), manifest, broken)
        raise AssertionError("未处理 worksheet.special_files 必须报错")
    except ValueError as exc:
        assert "未全部处理" in str(exc)

    broken = json.loads(json.dumps(answers))
    broken["specials"][0]["song_evidence"] = None
    try:
        plan_scaffold.apply_todo(json.loads(json.dumps(draft)), manifest, broken)
        raise AssertionError("credit 缺 song_evidence 必须报错")
    except ValueError as exc:
        assert "song_evidence" in str(exc)

    # 冲突集号必须用 add_episodes 显式择一。
    manifest_conflict = {
        "root": str(source_root),
        "files": [
            {"rel_path": "EP01.mkv", "stem": "EP01", "subdir_special_hint": False,
             "hint": {"episode_number": 1, "is_special": False, "episode_type": "normal"}},
            {"rel_path": "EP02a.mkv", "stem": "EP02a", "subdir_special_hint": False,
             "hint": {"episode_number": 2, "is_special": False, "episode_type": "normal"}},
            {"rel_path": "EP02b.mkv", "stem": "EP02b", "subdir_special_hint": False,
             "hint": {"episode_number": 2, "is_special": False, "episode_type": "normal"}},
        ],
    }
    snapshot_two = json.loads(json.dumps(snapshot))
    snapshot_two["bangumi"]["subjects"]["253"]["episodes"] = [
        {"type_id": 0, "ep": 1, "sort": 1, "name": "第一話", "name_cn": "第一话",
         "airdate": "2020-01-05", "desc": "第一话简介", "duration": "00:24:00"},
        {"type_id": 0, "ep": 2, "sort": 2, "name": "第二話", "name_cn": "第二话",
         "airdate": "2020-01-12", "desc": "", "duration": "00:24:00"},
    ]
    draft_two = plan_scaffold.scaffold_tv(
        snapshot_two, manifest_conflict, args, load_test_config())
    assert draft_two["scaffold"]["worksheet"]["ambiguous_episodes"]
    answers_two = {
        "apply_schema": "anime-scraper-todo-apply-v1",
        "output_dir": str(source_root), "sorttitle": "测试动画 2020-01-05 测试动画",
    }
    try:
        plan_scaffold.apply_todo(draft_two, manifest_conflict, answers_two)
        raise AssertionError("冲突集号未解决必须报错")
    except ValueError as exc:
        assert "ambiguous_episodes 未解决" in str(exc)
    answers_two["add_episodes"] = [{
        "rel_path": "EP02a.mkv", "episode": 2, "title": "第二话",
        "airdate": "2020-01-12", "plot": "补齐简介",
    }]
    answers_two["skipped_normal"] = [
        {"rel_path": "EP02b.mkv", "reason": "重复版本,待人工"}]
    final_two, _ = plan_scaffold.apply_todo(draft_two, manifest_conflict, answers_two)
    video_by_episode = {
        ep["episode"]: ep["video_path"] for ep in final_two["episodes"]}
    assert video_by_episode[2].endswith("EP02a.mkv")
    print("  [ok] plan_scaffold --apply-todo sidecar 合并与护栏")


def run() -> None:
    print("[integration] 文件系统/CLI/图片测试:")
    with tempfile.TemporaryDirectory() as raw:
        td = Path(raw)
        with config_root(td / "config-root"):
            test_nfo_hardlink_update(td / "nfo_hardlink")
            test_link_library(td / "link")
            test_movie_tree(td / "movie_link")
            test_multi_episode_ova_tree(td / "ova_link")
            test_scrape_hardlink_config_contract(td / "scrape_contract")
            test_lockdata_guard(td / "lockdata_guard")
            test_no_overwrite_existing_library(td / "no_overwrite")
            test_scan_tree(td / "scantree")
            test_scan_cli_manifest_summary(td / "scan_cli")
            test_metadata_snapshot_cli(td / "metadata_snapshot")
            test_plan_scaffold(td / "plan_scaffold")
            test_artwork_build_request(td / "artwork_build_request")
            test_plan_scaffold_apply_todo(td / "plan_apply")
            test_scrape_dry_run_report_summary(td / "dry_run_report")
            test_show_staff_guard()
            test_episode_plot_guards(td / "plot_guards")
            test_show_plot_guards(td / "show_plot_guards")
            test_empty_primary_plot_dry_run_report(td / "empty_plot_report")
            test_special_order_guard(td / "special_order")
            test_special_titles_guard(td / "special_titles")
            test_source_only_preflight_guards(td / "source_preflight")
            test_tmdb_special_identity_guard(td / "tmdb_identity")
            test_artwork_visual_review(td / "artwork_review")
            test_artwork_original_cache(td / "artwork_original_cache")
            test_artwork_projection(td / "artwork")
            test_incremental_artwork_update(td / "artwork_update")
            test_cached_replace_cli_shortcut(td / "cached_replace_shortcut")
            test_cached_replace_noop_and_review_sync(td / "cached_replace_sync")
            test_cached_replace_direct_main(td / "cached_replace_direct_main")
            test_cached_replace_direct_specials(td / "cached_replace_direct_specials")
            test_cached_replace_direct_marker_pools(td / "cached_replace_direct_pools")
            test_artwork_relpath_extension_guard(td / "artwork_ext_guard")
            test_thumb_stem_guard_rejects_year_in_name(td / "thumb_guard")
            test_special_airdate_guard(td / "airdate_guard")
            test_artwork_explicit_source_only(td / "artwork_src_only")
            test_images(td / "images")
            test_image_download_recovery_and_workers(td / "image_recovery")
            test_bootstrap_run_target_guard(td / "bootstrap_guard")
            test_cli_help_is_side_effect_free(td / "cli_help")
            test_step_zero_machine_marker(td / "step_zero_marker")
            test_skill_root_config(td / "skill_root_config")
    test_bootstrap_runtime_location()
    print("[integration] PASSED")


if __name__ == "__main__":
    run()
