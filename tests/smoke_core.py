"""Fast offline checks for core data, NFO and cache behavior."""
from __future__ import annotations

from smoke_support import *

def test_assemble_tv(td: Path):
    """TV 单元:plan → MergedShow;正片+特殊集,含一个 null(跳过)。"""
    v1 = _touch(td / "S01" / "ep01.mkv")
    v2 = _touch(td / "SPs" / "NCED.mkv")
    plan = {
        "type": "tv",
        "output_dir": str(td),
        "show": {
            "title": "测试动画",
            "sorttitle": "测试动画 2020-01-05 测试动画",
            "rating": 8.7,
            "plot": "中文简介优先", "premiered": "2020-01-05",
            "studio": "某工作室",
            "actors": [{"name": "某导演", "role": "监督", "type": "Director",
                        "thumb": "http://img/d.jpg"}],
            "staff_status": "present",
            "anidb_aid": 9999, "bgm_id": 253,
        },
        "episodes": [
            {"category": "normal", "season": 1, "episode": 1, "title": "第一话中文",
             "plot": "plot1", "airdate": "2020-01-05", "runtime": 24,
             "video_path": str(v1)},
            {"category": "credit", "season": 0, "episode": 1, "title": "NCED",
             "anidb_epno": "C1", "video_path": str(v2)},
            {"category": "special", "season": 0, "episode": 2, "title": "没匹配上的特典",
             "video_path": None},   # 跳过
        ],
    }
    show, video_paths = match.assemble(plan)

    assert show.title == "测试动画"
    assert show.sorttitle == "测试动画 2020-01-05 测试动画"
    assert show.rating == 8.7
    assert show.plot == "中文简介优先" and show.studio == "某工作室"
    assert show.anidb_aid == 9999 and show.bgm_id == 253
    assert show.staff_status == "present"
    assert len(show.episodes) == 3
    assert show.episodes[0].category == "normal" and show.episodes[0].season == 1
    assert show.episodes[1].category == "credit" and show.episodes[1].anidb_epno == "C1"
    assert video_paths == [str(v1), str(v2), None]
    print("  [ok] assemble TV 结构正确")
    return show, video_paths, v1, v2

def test_nfo_tv(td: Path, show, video_paths, v1, v2):
    xml = nfo.build_tvshow_nfo(show)
    assert "<title>测试动画</title>" in xml
    assert "<sorttitle>测试动画 2020-01-05 测试动画</sorttitle>" in xml
    assert "<rating>8.7</rating>" in xml
    assert 'type="anidb"' in xml and 'type="bangumi"' in xml
    # crew 使用既有库格式写入带真实类型的 actor 卡片。
    assert "<name>某导演</name>" in xml and "<type>Director</type>" in xml, xml

    written = nfo.write_all(show, str(td),
                            episode_paths=[Path(p) if p else None for p in video_paths])
    paths = {p.resolve() for p in written}
    # 文件为中心:nfo 与视频同名同目录;None 项跳过
    assert (td / "tvshow.nfo").resolve() in paths
    assert v1.with_suffix(".nfo").resolve() in paths
    assert v2.with_suffix(".nfo").resolve() in paths
    # 跳过的第三集不应写任何文件(只 1 tvshow + 2 集 = 3)
    assert len(written) == 3, [str(p) for p in written]
    print("  [ok] nfo TV 落盘正确(文件为中心 + 跳过 null)")

def test_episode_plot_rules():
    """TMDB 状态如实保留；任何分集的空简介都省略 <plot>。"""
    # BD 特典短篇与 Menu 分属 special/other，但都属于 TMDB 无对应内容的 Season 0。
    for category, title in (("special", "古立特骑士格斗 第4回"),
                            ("other", "Blu-ray Menu 1"),
                            ("credit", "NCOP"),
                            ("trailer", "PV 1")):
        ep, root = _episode_xml(category=category, title=title, plot="",
                                tmdb_match_status="not_found")
        assert root.find("plot") is None, (category, nfo.build_episode_nfo(ep))
        assert root.findtext("title") == title
        assert root.findtext("season") == "0" and root.findtext("episode") == "1"
        assert root.findtext("runtime") == "2" and root.findtext("anidb_epno") == "S1"

    # TMDB 有对应条目的特殊集继续保留真实简介。
    _, matched = _episode_xml(tmdb_match_status="matched", plot="TMDB 特殊集简介")
    assert matched.findtext("plot") == "TMDB 特殊集简介"

    # unknown（未检查/请求失败）不能被误判为 TMDB 不存在。
    _, unknown = _episode_xml(tmdb_match_status="unknown", plot="已有固定描述")
    assert unknown.findtext("plot") == "已有固定描述"

    # not_found 只表示 TMDB 无对应项，不能清空其它可靠来源已经确认的简介。
    _, not_found_with_plot = _episode_xml(
        tmdb_match_status="not_found", plot="官方资料确认的特殊集简介"
    )
    assert not_found_with_plot.findtext("plot") == "官方资料确认的特殊集简介"

    # 节点是否存在只由 plot 内容决定，不能为省略空节点而伪造 not_found。
    for status in ("matched", "not_found", "unknown"):
        _, empty = _episode_xml(tmdb_match_status=status, plot="")
        assert empty.find("plot") is None, status
    _, whitespace = _episode_xml(tmdb_match_status="unknown", plot=" \n  ")
    assert whitespace.find("plot") is None
    _, normal_empty = _episode_xml(category="normal", season=1,
                                   tmdb_match_status="unknown", plot="")
    assert normal_empty.find("plot") is None

    # 即使误给正片 not_found，也不能影响正片输出规则。
    _, normal = _episode_xml(category="normal", season=1,
                             tmdb_match_status="not_found", plot="正片简介")
    assert normal.findtext("plot") == "正片简介"
    print("  [ok] TMDB 状态与 plot 节点解耦，空简介统一省略")

def test_tmdb_match_status_assembly():
    """TMDB 三态从 plan 透传；缺失/非法值保守归一为 unknown。"""
    plan = {
        "show": {"title": "测试动画"},
        "episodes": [
            {"category": "special", "season": 0, "episode": 1,
             "title": "有对应条目", "tmdb_match_status": "matched"},
            {"category": "other", "season": 0, "episode": 2,
             "title": "无对应条目", "tmdb_match_status": "not_found"},
            {"category": "special", "season": 0, "episode": 3,
             "title": "缺状态字段"},
            {"category": "special", "season": 0, "episode": 4,
             "title": "非法状态", "tmdb_match_status": "missing"},
        ],
    }
    show, _ = match.assemble(plan)
    assert [ep.tmdb_match_status for ep in show.episodes] == [
        "matched", "not_found", "unknown", "unknown"
    ]
    print("  [ok] TMDB 匹配三态透传、缺失与非法值归一正确")

def test_movie(td: Path):
    vm = _touch(td / "剧场版.mkv")
    plan = {
        "type": "movie",
        "output_dir": str(td),
        "movie": {
            "title": "某剧场版",
            "sorttitle": "某剧场版 2023-03-24 某剧场版",
            "rating": "7.9",
            "plot": "电影简介", "premiered": "2023-03-24",
            "anidb_aid": 111, "bgm_id": 222, "video_path": str(vm),
        },
    }
    show, _extras = match.assemble_movie(plan)
    xml = nfo.build_movie_nfo(show)
    assert "<movie>" in xml and "<title>某剧场版</title>" in xml
    assert "<sorttitle>某剧场版 2023-03-24 某剧场版</sorttitle>" in xml
    assert show.rating == 7.9 and "<rating>7.9</rating>" in xml
    assert "<year>2023</year>" in xml
    assert 'type="anidb"' in xml and 'type="bangumi"' in xml

    out = nfo.write_movie(show, video_path=str(vm))
    assert out.resolve() == vm.with_suffix(".nfo").resolve()
    assert out.exists()
    minimal = match._show_from({"title": "最小条目"})
    assert minimal.sorttitle == ""
    assert minimal.rating is None
    assert "<sorttitle>" not in nfo.build_movie_nfo(minimal)
    assert "<rating>" not in nfo.build_movie_nfo(minimal)
    for invalid in (0, -1, 10.1, "nan", "bad", True):
        invalid_show = match._show_from({"title": "非法评分", "rating": invalid})
        assert invalid_show.rating is None, invalid
        assert "<rating>" not in nfo.build_movie_nfo(invalid_show), invalid
    print("  [ok] 剧场版 movie.nfo 正确")

def test_sorttitle_guard():
    """sorttitle 只校验结构；外部系列前缀交给 Agent 语义审查。"""
    valid = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "tv",
        "show": {
            "title": "真盖塔 世界最后之日",
            "sorttitle": "真盖塔 1998-08-25 真盖塔 世界最后之日",
            "premiered": "1998-08-25",
        },
    }
    result = scrape._validate_sorttitle(valid)
    assert result == {"status": "passed", "prefix": "真盖塔",
                      "sort_date": "1998-08-25", "prefix_in_title": True,
                      "semantic_review_required": False}

    haruhi = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "movie",
        "movie": {
            "title": "凉宫春日的消失",
            "sorttitle": "凉宫春日 2010-02-06 凉宫春日的消失",
            "premiered": "2010-02-06",
        },
    }
    haruhi_result = scrape._validate_sorttitle(haruhi)
    assert haruhi_result["prefix"] == "凉宫春日"
    assert haruhi_result["prefix_in_title"] is True
    assert haruhi_result["semantic_review_required"] is False

    gundam = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "tv",
        "show": {
            "title": "∀高达",
            "sorttitle": "机动战士高达 1999-04-09 ∀高达",
            "premiered": "1999-04-09",
        },
    }
    gundam_result = scrape._validate_sorttitle(gundam)
    assert gundam_result["prefix"] == "机动战士高达"
    assert gundam_result["prefix_in_title"] is False
    assert gundam_result["semantic_review_required"] is True

    # 跨语言错位护栏：中文 title 不得用罗马字系列前缀（SHIROBAKO 教训）
    shirobako = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "tv",
        "show": {
            "title": "白箱",
            "sorttitle": "SHIROBAKO 2014-10-09 白箱",
            "premiered": "2014-10-09",
        },
    }
    try:
        scrape._validate_sorttitle(shirobako)
        raise AssertionError("中文 title + 罗马字前缀应被拒绝")
    except ValueError as exc:
        assert "文字系统错位" in str(exc), str(exc)

    getter = json.loads(json.dumps(valid, ensure_ascii=False))
    getter["show"]["sorttitle"] = "GETTER 1998-08-25 真盖塔 世界最后之日"
    try:
        scrape._validate_sorttitle(getter)
        raise AssertionError("中文 title + 罗马字前缀应被拒绝")
    except ValueError as exc:
        assert "文字系统错位" in str(exc), str(exc)

    gekijo = {
        "plan_schema": scrape.PLAN_SCHEMA,
        "type": "movie",
        "movie": {
            "title": "白箱 剧场版",
            "sorttitle": "白箱 2020-02-29 白箱 剧场版",
            "premiered": "2020-02-29",
        },
    }
    assert scrape._validate_sorttitle(gekijo)["prefix"] == "白箱"
    assert scrape._validate_sorttitle(gekijo)["prefix_in_title"] is True

    full_title = json.loads(json.dumps(valid, ensure_ascii=False))
    full_title["show"]["sorttitle"] = (
        "真盖塔 世界最后之日 1998-08-25 真盖塔 世界最后之日"
    )
    assert scrape._validate_sorttitle(full_title)["prefix"] == "真盖塔 世界最后之日"

    year_only = json.loads(json.dumps(valid, ensure_ascii=False))
    year_only["show"]["premiered"] = "1998"
    year_only["show"]["sorttitle"] = (
        "真盖塔 1998-01-01 真盖塔 世界最后之日"
    )
    assert scrape._validate_sorttitle(year_only)["sort_date"] == "1998-01-01"

    unknown_date = json.loads(json.dumps(valid, ensure_ascii=False))
    unknown_date["show"]["premiered"] = ""
    unknown_date["show"]["sorttitle"] = (
        "真盖塔 9999-12-31 真盖塔 世界最后之日"
    )
    assert scrape._validate_sorttitle(unknown_date)["sort_date"] == "9999-12-31"

    wrong_schema = {
        "plan_schema": "invalid-plan-schema",
        "type": "tv",
        "show": {
            "title": "条目",
            "sorttitle": "条目 2020-01-01 条目",
            "premiered": "2020-01-01",
        },
    }
    for bad in (wrong_schema, {"type": "tv"}):
        try:
            scrape._require_plan_schema(bad)
            raise AssertionError("非当前契约的 plan_schema 应被拒绝")
        except ValueError as exc:
            assert "plan_schema 必须为" in str(exc), str(exc)
    for invalid_type in (None, "TV", "tv ", "movie ", "unknown", 1, True):
        invalid = json.loads(json.dumps(valid, ensure_ascii=False))
        invalid["type"] = invalid_type
        try:
            scrape._validate_plan_type(invalid)
            raise AssertionError(f"非法 plan.type 应被拒绝: {invalid_type!r}")
        except ValueError as exc:
            assert "plan.type 必须明确" in str(exc), str(exc)
    try:
        scrape._require_plan_schema([])
        raise AssertionError("非对象 plan 根应被拒绝")
    except ValueError as exc:
        assert "plan 根必须是对象" in str(exc), str(exc)
    print("  [ok] sorttitle 结构、外部系列前缀、plan_schema 与 plan.type 正确")


def test_image_decode_guard(td: Path):
    """非空但损坏的图片不能被当成有效 artwork。"""
    td.mkdir(parents=True, exist_ok=True)
    invalid = td / "invalid.jpg"
    invalid.write_bytes(b"not an image")
    assert not images._valid_image(invalid)
    valid = td / "valid.jpg"
    artwork_review.Image.new("RGB", (16, 24), (30, 60, 90)).save(valid, "JPEG")
    assert images._valid_image(valid)
    assert _common.decode_image_size(valid) == (16, 24)
    assert _common.decode_image_size(valid.read_bytes()) == (16, 24)
    print("  [ok] artwork 必须是可解码图片")


# ── test_match_specials / test_match_specials_other_guard 已删除 ──
# 特殊集匹配由 agent 运行时语义完成(match.py 不含匹配函数),不是可离线单测的脚本
# 函数;匹配规则见 references/special-rules.md。

def test_people_nfo():
    """人员渲染:声优/crew 用 actor, staff_note 进入简介末尾。"""
    show = match._show_from({
        "title": "T", "plot": "简介正文",
        "actors": [
            {"name": "声优X", "role": "角色1", "thumb": "http://img/x.jpg"},
            {"name": "导演A", "role": "导演", "thumb": "http://img/a.jpg", "type": "Director"},
            {"name": "脚本B", "role": "脚本", "thumb": "http://img/b.jpg", "type": "Writer"},
        ],
        "staff_note": "音乐:作曲家Y",
        "anidb_aid": 1, "bgm_id": 2,
    })
    xml = nfo.build_tvshow_nfo(show)
    assert "<actor>" in xml and "<name>声优X</name>" in xml, xml
    assert "<role>角色1</role>" in xml and "<thumb>http://img/x.jpg</thumb>" in xml, xml
    assert "<type>Actor</type>" in xml, xml
    assert "<type>Director</type>" in xml and "<type>Writer</type>" in xml, xml
    assert "音乐:作曲家Y" in xml, xml
    assert "<plot>简介正文\n\n音乐:作曲家Y</plot>" in xml, xml
    assert xml.count("<name>导演A</name>") == 1, xml
    print("  [ok] 人员渲染(actor + 简介末尾 staff 行 + 审计附注)正确")

def test_actor_cap_and_order():
    """每条目最多 20 位唯一声优，且声优始终位于 crew 前。"""
    cards = [
        {"name": "导演甲", "role": "导演", "type": "Director"},
        *[{"name": f"声优{i}", "role": f"角色{i}", "type": "Actor",
           "bangumi_person_id": i} for i in range(21)],
        {"name": "声优零别名", "role": "追加角色", "type": "Actor",
         "bangumi_person_id": 0},
        {"name": "编剧乙", "role": "脚本", "type": "Writer"},
    ]
    shows = (match.assemble({"show": {"actors": cards}})[0],
             match.assemble_movie({"movie": {"actors": cards}})[0])
    for show in shows:
        assert [card["type"] for card in show.actors] == ["Actor"] * 20 + ["Director", "Writer"]
        assert show.actors[0]["role"] == "角色0、追加角色"
    print("  [ok] 每条目唯一声优最多 20 位且始终位于 crew 前")

def test_episode_credits():
    """脚本按集拆分:'A(1,3,7,13)、B(2,6,10)' → 各集自己的 <credits>,未标注回退默认。"""
    per_ep, defaults = match.parse_episode_credits(
        "綾奈ゆにこ(1,3,7,13)、後藤みどり(2,6,10)、小川ひとみ(4,8,11)、和場明子(5,9,12)")
    assert per_ep[1] == ["綾奈ゆにこ"] and per_ep[7] == ["綾奈ゆにこ"], dict(per_ep)
    assert per_ep[6] == ["後藤みどり"], dict(per_ep)
    assert defaults == [], defaults
    # 区间写法 + 默认回退
    pe2, df2 = match.parse_episode_credits("甲(1-3)、乙")
    assert pe2[2] == ["甲"] and df2 == ["乙"], (dict(pe2), df2)
    assert match.episode_writers(5, pe2, df2) == ["乙"], "未标注集应回退默认"
    # 渲染进单集 nfo
    ep = match.MergedEpisode(category="normal", season=1, episode=1, title="第一话",
                             writers=["綾奈ゆにこ"])
    xml = nfo.build_episode_nfo(ep)
    assert "<credits>綾奈ゆにこ</credits>" in xml, xml
    print("  [ok] 每集脚本拆分 + 单集 <credits> 渲染正确")

def test_build_crew():
    """crew:能映射的做卡片(真类型+原职位名),无干净类型进审计附注,跳过公司。"""
    persons = [
        {"name": "柿本広大", "relation": "导演", "image": "http://i/k.jpg", "kind": 1},
        {"name": "柿本広大", "relation": "音响监督", "image": "http://i/k.jpg", "kind": 1},
        {"name": "綾奈ゆにこ", "relation": "系列构成", "image": "http://i/a.jpg", "kind": 1},
        {"name": "綾奈ゆにこ", "relation": "脚本", "image": "http://i/a.jpg", "kind": 1},
        {"name": "藤田淳平", "relation": "音乐", "image": "", "kind": 1},
        {"name": "某公司", "relation": "导演", "image": "", "kind": 2},          # 公司→跳过
        {"name": "茶之原拓也", "relation": "人物设定", "image": "http://i/c.jpg", "kind": 1},  # 无类型→附注
        {"name": "路人", "relation": "助理制片人", "image": "", "kind": 1},        # 非白名单→忽略
    ]
    cards, note = match.build_crew(persons)
    got = {c["name"]: (c["role"], c["type"]) for c in cards}
    assert got["柿本広大"] == ("导演、音响监督", "Director"), got     # 随附无类型职位并入 role
    assert got["綾奈ゆにこ"] == ("系列构成、脚本", "Writer"), got      # 同人多职位合并
    assert got["藤田淳平"] == ("音乐", "Composer"), got
    assert "茶之原拓也" not in got, "无干净类型应进附注,不做卡片"
    assert "某公司" not in got and "路人" not in got, got
    assert cards[0]["name"] == "柿本広大", "导演排最前"
    assert "人物设定:茶之原拓也" in note, note
    show = match._show_from({"title": "T", "actors": cards, "staff_note": note})
    xml = nfo.build_tvshow_nfo(show)
    assert ("<name>柿本広大</name>" in xml
            and "<type>Director</type>" in xml
            and "<name>綾奈ゆにこ</name>" in xml
            and "<type>Writer</type>" in xml
            and "<name>藤田淳平</name>" in xml
            and "<type>Composer</type>" in xml), xml
    assert "人物设定:茶之原拓也" in xml, xml
    assert "<plot>人物设定:茶之原拓也</plot>" in xml, xml
    print("  [ok] build_crew(真类型卡片 + 无类型进审计附注 + 跳过公司)正确")

def test_cached_people_repair_inputs():
    """原始 Bangumi 缓存必须恢复头像、声优和可映射 crew。"""
    characters = bangumi.normalize_characters([{
        "id": 10, "name": "角色甲", "relation": "主角",
        "images": {"large": "character.jpg"},
        "actors": [{"id": 20, "name": "声优甲",
                    "images": {"medium": "voice.jpg"}}],
    }])
    persons = bangumi.normalize_persons([{
        "id": 30, "name": "导演甲", "relation": "导演", "type": 1,
        "images": {"medium": "director.jpg"},
    }])
    show = {"title": "缓存修复", "actors": [None]}
    match.populate_show_staff(
        show, persons, characters=characters, localize=False,
    )
    assert show["actors"][0] == {
        "name": "声优甲", "role": "角色甲", "thumb": "voice.jpg",
        "type": "Actor", "bangumi_person_id": 20,
    }
    assert show["actors"][1] == {
        "name": "导演甲", "role": "导演", "thumb": "director.jpg",
        "type": "Director",
    }
    assert show["staff_audit"]["mappable_crew_count"] == 1
    print("  [ok] 原始 Bangumi people cache 可恢复声优/crew/头像")

def test_bangumi_name_localization(td: Path):
    """人员名/角色名:detail 简中 → 原名，且只解析最终入选项。"""
    td.mkdir(parents=True, exist_ok=True)
    cfg = {}

    assert bangumi._simplified_name_from_infobox([
        {"key": "简体中文名", "value": "千反田爱瑠"}
    ]) == "千反田爱瑠"

    original_character_detail = bangumi.get_character_detail
    original_person_detail = bangumi.get_person_detail
    try:
        bangumi.get_character_detail = lambda entity_id, _cfg=None: {
            "infobox": ([{"key": "简体中文名", "value": "千反田爱瑠"}]
                        if entity_id == 14823 else [])}
        bangumi.get_person_detail = lambda entity_id, _cfg=None: {
            "infobox": ([{"key": "简体中文名", "value": "佐藤聪美"}]
                        if entity_id == 5003 else [])}

        unresolved = []
        assert bangumi.resolve_display_name(
            "characters", 14823, "千反田える", cfg, unresolved) == "千反田爱瑠"
        assert bangumi.resolve_display_name(
            "persons", 5003, "佐藤聡美", cfg, unresolved) == "佐藤聪美"
        assert bangumi.resolve_display_name(
            "persons", 9999, "原名保留", cfg, unresolved) == "原名保留"
        assert unresolved == [{"kind": "persons", "id": 9999, "name": "原名保留"}]

        actors = match.build_actors([{
            "id": 14823, "name": "千反田える", "relation": "主角",
            "actors": [{"id": 5003, "name": "佐藤聡美", "image": "cv.jpg"}],
        }], cfg=cfg)
        assert actors == [{
            "name": "佐藤聪美", "role": "千反田爱瑠", "thumb": "cv.jpg",
            "type": "Actor", "bangumi_person_id": 5003,
        }]

        calls = []
        original_resolver = bangumi.resolve_display_name
        try:
            def fake_resolver(kind, entity_id, original, _cfg=None, _unresolved=None):
                calls.append((kind, entity_id))
                return {1: "中文主角", 11: "中文声优", 21: "中文导演"}.get(entity_id, original)
            bangumi.resolve_display_name = fake_resolver
            limited = match.build_actors([
                {"id": 1, "name": "主角", "relation": "主角",
                 "actors": [{"id": 11, "name": "声优", "image": ""}]},
                {"id": 2, "name": "配角", "relation": "配角",
                 "actors": [{"id": 12, "name": "声优二", "image": ""}]},
            ], limit=1, cfg=cfg)
            assert limited[0]["role"] == "中文主角"
            assert calls == [("persons", 11), ("characters", 1)], calls

            calls.clear()
            cards, _ = match.build_crew([
                {"id": 21, "name": "原导演", "relation": "导演", "image": "x", "kind": 1},
                {"id": 21, "name": "原导演", "relation": "音响监督", "image": "x", "kind": 1},
                {"id": 22, "name": "忽略者", "relation": "助理", "image": "", "kind": 1},
            ], cfg=cfg)
            assert cards[0]["name"] == "中文导演"
            assert calls == [("persons", 21)], calls

            calls.clear()
            match.build_crew([{"name": "无ID条目", "relation": "导演",
                               "image": "", "kind": 1}])
            assert calls == [], "无 Bangumi ID 的条目不得触发名字解析"
        finally:
            bangumi.resolve_display_name = original_resolver
    finally:
        bangumi.get_character_detail = original_character_detail
        bangumi.get_person_detail = original_person_detail
    print("  [ok] Bangumi 人员/角色简体中文名解析与原名回退正确")

def test_bangumi_search_cli():
    """Bangumi 搜索 CLI 只负责格式化，网络取数由模块函数提供。"""
    original_search = bangumi_search.search_subjects
    calls = []

    def fake_search(keyword, cfg=None, limit=10):
        calls.append((keyword, cfg, limit))
        return [{
            "id": 769,
            "name": "Top o Nerae!",
            "name_cn": "飞跃巅峰！",
            "date": "1988-10-07",
            "score": 8.1,
        }]

    try:
        bangumi_search.search_subjects = fake_search
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            assert bangumi_search.main([
                "Top", "o", "Nerae!", "--limit", "3", "--json"
            ]) == 0
        assert calls == [("Top o Nerae!", None, 3)], calls
        assert json.loads(output.getvalue())[0]["id"] == 769

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            assert bangumi_search.main(["Top o Nerae!"]) == 0
        assert "[769] 飞跃巅峰！ / Top o Nerae!" in output.getvalue()

        error_output = io.StringIO()
        with contextlib.redirect_stderr(error_output):
            try:
                bangumi_search.main(["Top", "--limit", "0"])
                raise AssertionError("limit=0 必须被拒绝")
            except SystemExit as exc:
                assert exc.code == 2
        assert "limit 必须是正整数" in error_output.getvalue()
    finally:
        bangumi_search.search_subjects = original_search
    print("  [ok] Bangumi 搜索 CLI 参数与 JSON/文本输出正确")

def test_theme_special_title():
    """OP/ED 特殊集命名:单个不带号、多个带号、有歌名接' - 歌名'、无歌名裸出。"""
    op = [{"display": "壱雫空"}]
    assert match.theme_special_title("op", op, 0, 1) == "OP - 壱雫空"          # 单个
    assert match.theme_special_title("ed", [], 0, 1) == "ED"                   # 无歌名裸出
    two = [{"display": "歌A"}, {"display": "歌B"}]
    assert match.theme_special_title("op", two, 0, 2) == "OP1 - 歌A"           # 多个带号
    assert match.theme_special_title("op", two, 1, 2) == "OP2 - 歌B"
    print("  [ok] theme_special_title(OP/ED 命名规则)正确")

def test_song_evidence_guard():
    """credit 必须有紧凑证据；裸 ED 不得只查 Bangumi 就停止。"""
    def make_plan(title="ED", status=None, sources=None, note="对应不明"):
        episode = {"category": "credit", "title": title, "video_path": "X:/ed.mkv"}
        if status:
            episode["song_evidence"] = {
                "status": status, "sources": sources or [], "note": note,
            }
        return {"plan_schema": scrape.PLAN_SCHEMA, "type": "tv", "episodes": [episode]}

    resolved = make_plan("ED - 刻司ル十二ノ盟約", "resolved", ["official:https://example.invalid"])
    result = scrape._validate_song_evidence(resolved)
    assert result["status"] == "passed" and result["resolved"] == 1
    exhausted = make_plan("ED", "exhausted", ["bangumi:123", "wiki:https://example.invalid"])
    assert scrape._validate_song_evidence(exhausted)["exhausted"] == 1

    invalid_plans = [
        make_plan(),
        make_plan(status="exhausted", sources=["bangumi:123"]),
        make_plan(status="resolved", sources=["bangumi:123"]),
        make_plan("ED - 猜测歌名", "exhausted", ["wiki:https://example.invalid"]),
    ]
    for invalid in invalid_plans:
        try:
            scrape._validate_song_evidence(invalid)
            raise AssertionError("缺证据或只查 Bangumi 的裸 ED 应被拒绝")
        except ValueError:
            pass
    print("  [ok] OP/ED song_evidence 护栏正确")

def test_pure_specials_tv_guard():
    """纯 Season 0 必须归入已有正片，不能独立建 TV 库条目。"""
    invalid = {
        "type": "tv",
        "episodes": [{"category": "special", "season": 0, "episode": 1,
                      "title": "SP", "video_path": "X:/sp.mkv"}],
    }
    try:
        scrape._validate_tv_primary_content(invalid)
        raise AssertionError("纯 SP TV plan 应被拒绝")
    except ValueError as exc:
        assert "纯 Season 0/SP" in str(exc)

    valid = {
        "type": "tv",
        "episodes": [{"category": "normal", "season": 1, "episode": 1,
                      "title": "正片", "video_path": "X:/ep01.mkv"},
                     {"category": "special", "season": 0, "episode": 1,
                      "title": "SP", "video_path": "X:/sp.mkv"}],
    }
    assert scrape._validate_tv_primary_content(valid)["primary_count"] == 1
    print("  [ok] 纯 SP TV 条目被拒绝，必须归入已有正片")

def test_common_paths():
    """公共路径规范化与 config 默认源/硬链接目录解析。"""
    from _common import hardlink_library_enabled, link_root, normalize_path, source_root
    assert normalize_path(r"\\NAS\Anime\Show") == "//NAS/Anime/Show"
    assert normalize_path("//NAS/Anime/Show") == "//NAS/Anime/Show"
    assert normalize_path(r"C:\Users\x\y") == "C:/Users/x/y"
    assert normalize_path(Path("a/b")) == "a/b"
    cfg = {
        "paths": {"source_root": "D:/Anime"},
        "library": {"hardlinks": {"enabled": True, "root": "D:/Jellyfin/Anime"}},
    }
    assert source_root(cfg) == Path("D:/Anime")
    assert hardlink_library_enabled(cfg) is True
    assert link_root(cfg) == Path("D:/Jellyfin/Anime")
    assert hardlink_library_enabled({"library": {"hardlinks": {"enabled": False}}}) is False
    assert link_root(cfg, override="E:/Library") == Path("E:/Library")
    for bad in ("yes", 1, None):
        if bad is None:
            continue
        try:
            hardlink_library_enabled({"library": {"hardlinks": {"enabled": bad}}})
            raise AssertionError("硬链接开关必须拒绝非布尔值")
        except ValueError:
            pass
    fallback = {
        "paths": {"source_root": "D:/Anime"},
        "library": {"hardlinks": {"enabled": True, "root": ""}},
    }
    assert link_root(fallback) == Path("D:/Anime/_Jellyfin")
    try:
        link_root({"library": {"hardlinks": {"enabled": True, "root": ""}}})
        raise AssertionError("root 与 source_root 同时为空时必须拒绝")
    except ValueError:
        pass
    print("  [ok] config 硬链接总开关与目标目录解析正确")

def test_tmdb_specials_poster_selection():
    """TMDB 排序、Season 0 404 语义与 Specials 三段回退均离线可验证。"""
    # 低票池按语言优先，不能让多一票的英文图压过中文图。
    low_pool = [_image_candidate("/en.jpg", language="en", votes=2),
                _image_candidate("/zh.jpg", language="zh", votes=1)]
    before = [item["file_path"] for item in low_pool]
    ranked = tmdb.rank_image_candidates(low_pool)
    assert ranked[0]["file_path"] == "/zh.jpg", ranked
    assert [item["file_path"] for item in low_pool] == before, "排序不得原地修改输入"

    # 同语言、低票时高分辨率优先；高票差仍以票数为主。
    resolution_pool = [_image_candidate("/small.jpg", language="zh", votes=1,
                                        width=600, height=800),
                       _image_candidate("/large.jpg", language="zh", votes=0,
                                        width=2000, height=3000)]
    assert tmdb.rank_image_candidates(resolution_pool)[0]["file_path"] == "/large.jpg"
    review_pool = [
        _image_candidate(f"/ranked-{index}.jpg", language="zh", votes=20 - index,
                         width=600, height=900)
        for index in range(5)
    ]
    resolution_challenger = _image_candidate(
        "/resolution-challenger.jpg", language="zh", votes=0,
        width=2000, height=3000,
    )
    review_candidates = tmdb.poster_review_candidates(
        [*review_pool, resolution_challenger]
    )
    assert resolution_challenger["file_path"] in {
        item["file_path"] for item in review_candidates
    }, "最高分辨率候选必须保留识图席位"
    localized_pool = [
        *review_pool,
        _image_candidate("/zh-localized.jpg", language="zh", votes=0,
                         width=1200, height=1800),
        _image_candidate("/ja-localized.jpg", language="ja", votes=0,
                         width=1400, height=2100),
        _image_candidate("/en-largest.jpg", language="en", votes=0,
                         width=2000, height=3000),
    ]
    localized_review = tmdb.poster_review_candidates(localized_pool)
    localized_paths = {item["file_path"] for item in localized_review}
    assert {"/zh-localized.jpg", "/ja-localized.jpg", "/en-largest.jpg"} <= localized_paths
    high_gap = [_image_candidate("/high.jpg", language="en", votes=10),
                _image_candidate("/low.jpg", language="zh", votes=2,
                                 width=3000, height=4500)]
    assert tmdb.rank_image_candidates(high_gap)[0]["file_path"] == "/high.jpg"

    main = _image_candidate("/main.jpg", language="ja", votes=4)
    alternative = _image_candidate("/alternative.jpg", language="zh", votes=1)
    season_zero = _image_candidate("/specials.jpg", language="en", votes=0)
    original_hash_distances = tmdb._image_hash_distances
    try:
        # 离线测试中固定为视觉不同，验证正常的 Season 0 优先级。
        tmdb._image_hash_distances = lambda *_args, **_kwargs: (64, 64, 64)
        selected = tmdb.select_specials_poster(
            main_poster=main, main_poster_candidates=[main, alternative],
            season_zero_poster_candidates=[season_zero])
        assert selected["selection"] == "season_zero"
        assert selected["candidate"]["file_path"] == "/specials.jpg"
        # Season 0 无竖图时，从完整主池按排序取与主图不同的第一张。
        selected = tmdb.select_specials_poster(
            main_poster=main, main_poster_candidates=[main, alternative],
            season_zero_poster_candidates=[_image_candidate("/landscape.jpg", width=1600, height=900)])
        assert selected["selection"] == "main_pool_alternative"
        assert selected["candidate"]["file_path"] == "/alternative.jpg"
        # 没有不同图时才复用主图。
        selected = tmdb.select_specials_poster(
            main_poster=main, main_poster_candidates=[main], season_zero_poster_candidates=[])
        assert selected["selection"] == "none" and selected["candidate"] is None

        # 回归：Season 0 的第一张若与主图同画，必须继续检查下一张，不能被优先分支绕过。
        def fake_dist(_main_url: str, candidate_url: str, **_kwargs) -> tuple[int, int, int]:
            return (4, 4, 4) if candidate_url.endswith("/near.jpg") else (26, 26, 26)
        tmdb._image_hash_distances = fake_dist
        selected = tmdb.select_specials_poster(
            main_poster=main,
            main_poster_candidates=[main],
            season_zero_poster_candidates=[
                _image_candidate("/near.jpg", votes=3),
                _image_candidate("/far.jpg", votes=1),
            ])
        assert selected["selection"] == "season_zero"
        assert selected["candidate"]["file_path"] == "/far.jpg"

        # 回归：同底图只替换 logo 时 pHash 可能偏高，但 aHash/wHash 仍接近。
        # 来自深渊第一季的实际误判样本距离为 (16, 5, 6)。
        tmdb._image_hash_distances = lambda *_args, **_kwargs: (16, 5, 6)
        selected = tmdb.select_specials_poster(
            main_poster=main,
            main_poster_candidates=[main],
            season_zero_poster_candidates=[season_zero],
        )
        assert selected["selection"] == "none" and selected["candidate"] is None

        # 回归：映像研实际样本距离为 (20, 12, 12)，提高 aHash/wHash 阈值后必须拒绝。
        tmdb._image_hash_distances = lambda *_args, **_kwargs: (20, 12, 12)
        selected = tmdb.select_specials_poster(
            main_poster=main,
            main_poster_candidates=[main],
            season_zero_poster_candidates=[season_zero],
        )
        assert selected["selection"] == "none" and selected["candidate"] is None

        # 三项必须同时越过阈值，边界值本身仍按相似图处理。
        for distances in ((16, 64, 64), (64, 16, 64), (64, 64, 16)):
            tmdb._image_hash_distances = lambda *_args, _d=distances, **_kwargs: _d
            selected = tmdb.select_specials_poster(
                main_poster=main,
                main_poster_candidates=[main],
                season_zero_poster_candidates=[season_zero],
            )
            assert selected["selection"] == "none" and selected["candidate"] is None, distances

        # 跨季度回归：SP 候选还必须区别于本系列其它季度主图。
        other_main = _image_candidate("/other-main.jpg")
        contaminated = _image_candidate("/contaminated.jpg", votes=3)
        valid = _image_candidate("/valid.jpg", votes=1)
        fallback_sp = _image_candidate("/season-one-specials.jpg")

        def cross_season_dist(reference_url: str, candidate_url: str,
                              **_kwargs) -> tuple[int, int, int]:
            if (reference_url.endswith("/other-main.jpg")
                    and candidate_url.endswith("/contaminated.jpg")):
                return 16, 5, 6
            return 26, 26, 26

        tmdb._image_hash_distances = cross_season_dist
        selected = tmdb.select_specials_poster(
            main_poster=main,
            main_poster_candidates=[main],
            season_zero_poster_candidates=[contaminated, valid],
            reference_main_posters=[other_main],
        )
        assert selected["selection"] == "season_zero"
        assert selected["candidate"]["file_path"] == "/valid.jpg"

        # 其它季度 SP 不参与排重；没有合格新图时允许复用已有 SP。
        tmdb._image_hash_distances = lambda *_args, **_kwargs: (4, 4, 4)
        selected = tmdb.select_specials_poster(
            main_poster=main,
            main_poster_candidates=[main],
            season_zero_poster_candidates=[contaminated],
            reference_main_posters=[other_main],
            fallback_specials_poster=fallback_sp,
        )
        assert selected["selection"] == "none" and selected["candidate"] is None

        def fallback_dist(_reference_url: str, candidate_url: str,
                          **_kwargs) -> tuple[int, int, int]:
            return (26, 26, 26) if candidate_url.endswith(
                "/season-one-specials.jpg") else (4, 4, 4)

        tmdb._image_hash_distances = fallback_dist
        selected = tmdb.select_specials_poster(
            main_poster=main,
            main_poster_candidates=[main],
            season_zero_poster_candidates=[contaminated],
            reference_main_posters=[other_main],
            fallback_specials_poster=fallback_sp,
        )
        assert selected["selection"] == "series_specials_reuse"
        assert selected["candidate"]["file_path"] == "/season-one-specials.jpg"
    finally:
        tmdb._image_hash_distances = original_hash_distances

    original = tmdb.get_season_images
    try:
        def fail_404(*_args, **_kwargs):
            raise tmdb.HTTPError(response=type("Response", (), {"status_code": 404})())
        tmdb.get_season_images = fail_404
        assert tmdb.get_optional_season_images(1, 0) == {
            "posters": [], "backdrops": [], "logos": []}

        def fail_429(*_args, **_kwargs):
            raise tmdb.HTTPError(response=type("Response", (), {"status_code": 429})())
        tmdb.get_season_images = fail_429
        try:
            tmdb.get_optional_season_images(1, 0)
            raise AssertionError("429 不得被误判为 Season 0 不存在")
        except tmdb.HTTPError:
            pass
    finally:
        tmdb.get_season_images = original
    print("  [ok] TMDB 候选排序、Season 0 404 语义与 Specials 三段选图正确")

def test_atomic_cache_writes(td: Path):
    """缓存原子替换成功，序列化/替换失败均保留旧内容且不留临时文件。"""
    target = td / "nested" / "sample.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    old_body = b'{"version": 1}\n'
    target.write_bytes(old_body)

    def temp_files():
        return list(target.parent.glob(f".{target.name}.*.tmp"))

    _common.atomic_write_json(target, {"version": 2, "title": "新缓存"})
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "version": 2, "title": "新缓存"
    }
    assert not temp_files()

    try:
        _common.atomic_write_json(target, {"invalid": object()})
        raise AssertionError("不可序列化缓存必须失败")
    except TypeError:
        pass
    assert json.loads(target.read_text(encoding="utf-8"))["version"] == 2
    assert not temp_files()
    preserved_body = target.read_bytes()

    original_replace = _common.os.replace

    def fail_replace(_temporary, _destination):
        raise OSError("injected replace failure")

    _common.os.replace = fail_replace
    try:
        try:
            _common.atomic_write_bytes(target, b"new body")
            raise AssertionError("替换失败必须向上抛出")
        except OSError as exc:
            assert "injected replace failure" in str(exc)
    finally:
        _common.os.replace = original_replace
    assert target.read_bytes() == preserved_body
    assert not temp_files()
    assert old_body != target.read_bytes(), "成功写入后的旧值应已被替换"
    print("  [ok] 缓存原子写入成功/失败保留旧文件且无残留临时文件")

def test_anidb_http_requires_registered_client(td: Path):
    cfg = {
        "cache_dir": str(td / "cache"),
        "anidb": {"http": {"client": "", "clientver": 1}},
    }
    assert anidb_episodes._http_available({
        "anidb": {"http": {"client": "registered-client", "clientver": 1}}
    })
    assert not anidb_episodes._http_available(cfg)
    try:
        anidb_episodes.get_episodes(999999, cfg)
        raise AssertionError("AniDB HTTP 缺少 client 时必须明确拒绝")
    except RuntimeError as exc:
        assert "AniDB HTTP API 未配置" in str(exc)
    print("  [ok] AniDB HTTP 缺少自行登记 client 时明确拒绝且不触网")

def run() -> None:
    print("[core] 离线核心测试:")
    with tempfile.TemporaryDirectory() as raw:
        td = Path(raw)
        show, vps, v1, v2 = test_assemble_tv(td / "assemble_tv")
        test_nfo_tv(td / "nfo_tv", show, vps, v1, v2)
        test_episode_plot_rules()
        test_tmdb_match_status_assembly()
        test_movie(td / "movie")
        test_sorttitle_guard()
        test_image_decode_guard(td / "image_decode")
        test_people_nfo()
        test_actor_cap_and_order()
        test_episode_credits()
        test_build_crew()
        test_cached_people_repair_inputs()
        test_bangumi_name_localization(td / "bangumi_names")
        test_bangumi_search_cli()
        test_atomic_cache_writes(td / "atomic_cache")
        test_anidb_http_requires_registered_client(td / "anidb_http")
    test_common_paths()
    test_theme_special_title()
    test_song_evidence_guard()
    test_pure_specials_tv_guard()
    test_tmdb_specials_poster_selection()
    print("[core] PASSED")


if __name__ == "__main__":
    run()
