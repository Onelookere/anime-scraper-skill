# anime-scraper-skill · 动画刮削

> 用 **AI 当识别引擎**配合多个在线数据源的动画刮削器：为压制组发布的动画片源(BDRip/BDMV)生成 Kodi / Jellyfin / Emby 通用的 `.nfo`，并可选建立硬链接媒体库。

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

本工具用于在 Claude Code / Codex 等 agent 里使用：认番、匹配、判置信由 agent 在运行时完成，Python 脚本只做扫描、取数、校验和落盘。全部工作流程与规则写在 [`SKILL.md`](SKILL.md) 和 [`references/`](references/)，供 agent 读取。

| 内容 | 数据源 |
|---|---|
| 正片(集号 / 简介 / staff / 分集标题) | Bangumi |
| 特殊集(SP / OP / ED / NCOP / NCED / BD 特典)结构 | AniDB |
| 封面 / 海报 / 分集简介兜底 | TMDB |

## 安装

将本仓库 clone 到本地的 skill 目录（如 `~/.claude/skills/`）即可，或者直接把项目地址提供给 agent 让它安装。

初次使用前需要编辑 `config.json` 完成如下配置。

## 配置

编辑 skill 根目录下的 `config.json`

| 字段 | 说明 |
|---|---|
| `paths.source_root` | 默认读取的动画源目录；可留空，留空时每次刮削显式给出源目录即可 |
| `library.hardlinks.enabled` | 是否建立硬链接媒体库。建议开启：压制组原始目录结构杂乱，硬链接树会将其整理为 Jellyfin / Emby 可识别的标准库结构，且不占额外磁盘空间 |
| `library.hardlinks.root` | 硬链接库的目标根目录；留空时默认使用 `<source_root>/_Jellyfin` |
| `artwork.multimodal_review` | 是否开启多模态识图筛选海报（需模型本身支持） |
| `artwork.artwork_cache` | 是否把符合条件的 TMDB 海报原图缓存到本地，方便人工筛选替换 |
| `artwork.tmdb/ffmpeg_workers` | 海报获取并发进程数，建议保持默认 |
| `anidb.http.client(ver)` | AniDB 客户端标识；本项目已登记公开 client ，保持默认即可 |
| `bangumi.access_token` | Bangumi 访问令牌（Access Token），在 <https://next.bgm.tv/demo/access-token> 免费获取 |
| `bangumi.user_agent` | API 请求的 User-Agent 标识，格式如 `你的用户名/anime-scraper` |
| `tmdb.access_token` | TMDB API 读访问令牌（v4，`eyJ` 开头的长串），在 <https://www.themoviedb.org/settings/api> 免费获取 |
| `cache_dir` | API 缓存目录 |
| `rate` | API 请求速率限制，建议保持默认 |

## 使用

对 agent 说「刮削动画 <片源目录>」即可，其余由 agent 按 `SKILL.md` 编排。

更推荐使用 `/anime-scraper` 命令显式调用。

```bash
# 离线自测(无需密钥、不联网),打印 ALL PASSED 即正常
python scripts/bootstrap.py --run tests/smoke_test.py
```

首次运行时，会在 skill 目录内的 `.runtime/` 自动创建隔离的 Python 虚拟环境并安装相关依赖

考虑到运行环境差异，初次使用时请先做一次文件备份，防止可能的风险

bangumi的数据接口国内可能无法直连，anidb的接口有时直连反而更稳定

刮削已经拿到了大部分元数据，在jellyfin等软件创建媒体库的时候可以考虑直接禁止联网获取数据，否则可能会有一些奇怪的覆盖问题。媒体库类型选择混合电影和电视节目

## 限流与合规

- AniDB 限流严格，违规 ban 可长达 12 小时。内置参考 [ShokoServer](https://github.com/ShokoAnime/ShokoServer) 的两档限流器与本地缓存
- 需要调整请求频率时，请在 [AniDB](https://anidb.net/software/add) 自己独立注册一个 client 并替换，不要复用本项目登记的 `animescraper`。（数据源侧限流严格，调高频率可能不会有什么效果）
- 本工具**只刮取公开元数据，不下载、不分发任何影视内容**，使用者需遵守各数据源的条款与限流规则。

## 数据源归属

<a href="https://anidb.net"><img src="https://cdn.anidb.net/css/assets/images/touch/android-chrome-512x512.png" alt="AniDB" width="72"></a>&nbsp;&nbsp;
<a href="https://bgm.tv"><img src="https://bgm.tv/img/logo_riff.png" alt="Bangumi 番组计划" width="200"></a>&nbsp;&nbsp;
<a href="https://www.themoviedb.org"><img src="https://www.themoviedb.org/assets/2/v4/logos/v2/blue_long_2-9665a76b1ae401a510ec1e0ca40ddcb3b0cfe45f1d51b77a308fea0845885648.svg" alt="The Movie Database (TMDB)" width="220"></a>

## License

[MIT](LICENSE) © 2026 anime-scraper contributors
