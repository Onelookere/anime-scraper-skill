# 增量维护、预算与恢复

仅在用户维护已有成果、且主 `SKILL.md` 的增量条件满足时读取本文件。它不参与首次完整刮削。

## 1. 何时使用

增量适用于目标可精确枚举、作品身份/单元切分/库拓扑不变、无需重新识别整包，且新增或修改媒体条目不超过 10 个的维护。以下任一情况回到完整流程：

- 作品认定、TV/Movie 类型、单元切分、正片/Specials 归属、集号或库目录改变；
- 缺少可信 plan/manifest，无法列出全部受影响目标（已有缓存海报可走 §2 的无 plan 快捷模式）；
- 新规则需要追溯整包，或用户明确要求整季、整作、全库刷新。

共享代码可以用来计算期望值，但只能写入 change set 白名单。追加 Season 0 时必须重新读取库侧最大 `S00E`，从其后连续编号。

## 2. Change set 与缓存海报快捷替换

普通增量只建立一个 change set，记录模式、修改原因、每项操作的源/目标、旧状态、新状态、资源预算、禁止触碰对象、验证项目和停止条件。终端只显示有界摘要，完整 change set/report 留本机。

change set 即目标白名单。实际目标数、下载/probe 数、操作类型或写路径超出预算时立即停止未执行写入并重新审查，不得静默扩容。

已有 artwork cache 候选时，单张海报可直接使用：

```text
python scripts/bootstrap.py --run scripts/update_artwork.py --cached-replace --plan <plan.json> --candidate-id G01C01 --target main-poster|specials-poster
```

没有 plan 时显式提供 `--source-dir`、精确 `--library-dir` 和 `--original-cache-dir`。快捷入口不扫描、不取元数据、不运行整单元 dry-run、不生成临时 change set；下载与 ffmpeg 预算为 0，只处理固定白名单并在同一次调用中完成候选解码、原子替换和源/库 inode 验证。候选 API 尺寸不是最终尺寸，报告以 Pillow 解码尺寸为准。

`main-poster` 白名单为源/库根 poster 与已存在的直系季 poster；`specials-poster` 白名单为源 `specials-poster.jpg` 与库 `Specials/poster.jpg`。成功后同步 plan artwork、`CURRENT` 标记和受影响库侧硬链接；无 plan 模式不写 plan，但按目标同步 `CURRENT` 标记：main-poster 对应 tv-show 组（无 tvshow.nfo 且无季目录时为 movie 组）与现有 season-XX 组，specials-poster 对应 specials 组；仅当该组候选池确实包含已装图片（含共享同一缓存文件的跨池别名）时改写标记，否则保持原状并在报告中说明。`Specials/poster.jpg` 直接按 replace 处理，目标级验证包含在同一次调用中。

change set 使用 `candidate_width/candidate_height` 记录 TMDB 候选尺寸；二者都不是最终解码尺寸。

## 3. 各类对象的最小写入

| 对象 | 增量写入 | 禁止 |
|---|---|---|
| NFO/结构化数据 | 解析并比较期望值，只写有差异的目标记录；共享字段变更时升级单元级检查 | 为改一集重写整季或用字符串拼接改 JSON/XML |
| 图片 | URL/原图按缓存复用；`update_artwork.py` 只替换白名单目标，缓存替换下载为 0 | 全季刷新、重截无关 thumb、删除有效源图后重下 |
| 硬链接 | Python `os.link` + 同目录 staging/`os.replace`，逐项核对 `st_dev + st_ino` | PowerShell 建链、跨卷复制、重建整树、未检查旧目标就删除 |
| 新增媒体 | 只扫描、probe、生成新增项并检查受影响编号/配对 | 因新增一项重 probe 或刷新既有全部媒体 |

依赖顺序固定为“plan/结构化数据 → 源侧实体 → 库侧硬链接”。共享 inode 的 NFO 先完整构建并校验 XML，再原位写源路径并确认两路内容/inode 仍一致；禁止只对源路径 `os.replace`，否则库侧会留在旧 inode。

## 4. 验证、恢复与测试预算

目标级验证始终执行：文件存在且非空、格式可解析、期望字段/图片可解码、涉及硬链接则核对 inode。改变编号、日期、共享作品字段或新增/删除 episode 时追加单元级验证；只有修改 skill 代码/契约或局部验证无法覆盖共享不变量时才做全量级测试。

中断恢复先读同一路径的既有 report，按每项 URL、实际解码尺寸/哈希和目标 inode 分类为已完成、待替换或待重链；混合状态立即停止，不重复下载或重建整树。纯数据落盘且未改 skill 代码时，不因“保险”重复跑完整 smoke test。

作品级 staff 修复使用 `scripts/repair_staff.py`，只在用户明确要求且 persons/characters 缓存已指定时运行；它不属于首次刮削入口。
