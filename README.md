---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 04759dec790a73195f38184c455774f3_ca410edcdcdf11f1a0ca525400de85a5
    ReservedCode1: i4f92874Ozp+SSbr0vajFvH5ckeYdAN0RAAa9q/dc2Dk1Xe8/6xOcM3NcqFyygEBr0rsVnu/oMwImuaUuQ7j3+pTatXx8uNtQ3EXC7xOXpKL/77zIt2vDbaoRk4SaxLTX/uOLOjUYlD11OF3HdGaOUYufywSXkdi6G5ZlUW7NnAiD4JXDiP8tOHnXMU=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 04759dec790a73195f38184c455774f3_ca410edcdcdf11f1a0ca525400de85a5
    ReservedCode2: i4f92874Ozp+SSbr0vajFvH5ckeYdAN0RAAa9q/dc2Dk1Xe8/6xOcM3NcqFyygEBr0rsVnu/oMwImuaUuQ7j3+pTatXx8uNtQ3EXC7xOXpKL/77zIt2vDbaoRk4SaxLTX/uOLOjUYlD11OF3HdGaOUYufywSXkdi6G5ZlUW7NnAiD4JXDiP8tOHnXMU=
---

# astrbot_plugin_ghfetch

**GitHub Release 加速下载** —— 在聊天里丢个 GitHub 链接或项目名，机器人在服务器上用 aria2 多线程抓下来，直接把文件回传给你。

群友要个软件，不用再翻墙、不用再传网盘、不用等对方发截图，一句「下载 xxx」就完事。

## 功能

- **三种输入都能认**
  - Release 链接：`https://github.com/owner/repo/releases/tag/v1.0.0`
  - 项目名：`下载 7-Zip` / `下载 Clash Verge`
  - 裸仓库链接：`https://github.com/owner/repo`（不发 Release，改发仓库简介 + README 总结）
- **多平台自动列菜单**：同一 Release 有 Windows / Android / macOS / Linux 多个包时，回一个带序号的菜单，回复数字自选；也可以直接说 `下载 xxx win`、`下载 xxx 安卓` 只看某个平台。
- **aria2 多线程加速**：默认 16 连接，走镜像前缀回退（直连 → ghfast.top → gh-proxy.com，可自定义），比浏览器直连稳得多。
- **下载完直接回传**：起一个内部 HTTP 服务把文件交给适配器上传，发完延迟一段时间自动删掉服务器残留，不占盘。
- **没有 Release 也不尴尬**：会先问一句「要不要发源码 zip」，你确认了才动手。
- **同名模糊时给候选**：搜到的项目不完全同名，列候选让你挑，不会乱下一堆。

## 安装

1. 把插件目录放进 `data/plugins/astrbot_plugin_ghfetch`，或在 Dashboard 插件市场安装。
2. 装好重启插件，在插件详情页的 **参数设置** 里把常用项调一下（有独立可视化页面，不用改 JSON）。
3. 服务器需要 `aria2c`：

```bash
# Debian / Ubuntu
apt install -y aria2
# Alpine
apk add aria2
```

> 没装 aria2 也能跑，只是退回单线程，速度看运气。

## 用法示例

```
下载 7-Zip                     # 按项目名搜，默认优先 Windows 资产
下载 clash verge win           # 指定平台
https://github.com/xxx/yyy/releases/tag/v1.2.3   # 直接发链接
下载 https://github.com/xxx/yyy                  # 裸仓库链接 → 简介 + README 总结
```

## 参数设置页

插件自带可视化设置页（Dashboard → 插件 → 本插件 → 参数设置），23 项分 6 组，改完点保存即可，部分项（端口、回传主机、aria2 路径等）需重启插件生效。

| 分组 | 关键项 | 说明 |
| --- | --- | --- |
| 基础开关 | `enabled` / `allow_group` / `allow_private` / `whitelist_groups` | 总开关、群聊/私聊白名单 |
| 下载与加速 | `aria2_path` / `connections` / `max_concurrent` / `max_size_mb` / `mirrors` | aria2 路径、并发连接数、同时任务数、单文件上限、镜像前缀顺序 |
| 回传与清理 | `http_port` / `callback_host` / `clean_delay` | 内部回传端口、回传主机名、发完多久删残留 |
| 识别与选择 | `win_prefer` / `ask_on_ambiguous` / `search_candidates` / `asset_candidates` / `aliases` / `pending_ttl` | 平台偏好、模糊时是否列候选、搜索与资产候选数量、项目别名、待选超时 |
| 仓库简介 | `repo_intro` / `repo_intro_llm` / `repo_intro_readme_chars` / `info_cache_ttl` | 裸仓库链接发不发简介、要不要 LLM 一句话总结、README 截取长度、缓存时长 |
| GitHub API | `github_token` | 填 PAT 后 API 限额从 60 次/时提到 5000 次/时 |

## 常见问题

**解析失败：GitHub API 限流了**
匿名调用 GitHub API 每小时只有 60 次，群聊人一多就打满。在设置页填一个只读 `github_token`（classic PAT 或 fine-grained 都行，不需要任何写权限）即可。

**文件发不出去 / 回传超时**
检查 `http_port`（默认 8899）在没有被别的东西占用，且 `callback_host` 能被 SnowLuma / 适配器访问到；两者在同一容器网络时留空自动探测即可。

**文件太大被拒**
`max_size_mb` 默认 500，超过会直接回一条直链，不走下载。

## 元数据

- 需要 AstrBot `>= 4.24.0`（插件自带页面与 Web API 能力）
- 主要适配平台：`aiocqhttp`

## License

MIT
*（内容由AI生成，仅供参考）*
Enter file contents here
