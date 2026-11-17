# 地理新闻预览：数据与许可

此目录是独立的本机只读设计预览，默认端口 **3008**。不替换正式入口，不导入正式应用或账户配置，不请求模型或新闻接口；浏览器运行时使用本地固定资源，不向 CDN 发送请求。

## 新闻、地理锚点和席位规则

- 15 条新闻均为人工编写的**虚构情景**，时间也是演示时间，不代表当天真实新闻。
- 14 条新闻有演示地理锚点，其中北京两条用于演示同地点聚合；另 1 条全球议题位置为 `null`，只保留在新闻列表，不能伪造地图位置。
- 坐标采用城市中心附近的概略坐标，包括首都以及深圳、杭州，仅为交互演示锚点，不表示真实新闻发生地，不是地理编码服务的输出。省市字段只用于地点显示、检索与定位；未知层级留空，底图不含省市行政边界。
- 新闻→板块、板块→模拟股票均为明确的人工演示映射，不是经验证的产业因果。红/绿表示所选新闻对特定板块的演示方向，不能解释成某个国家整体利好或利空。明暗等级是手工设置的序数，不是置信度或收益预测。
- 席位规则复用既有 `news-network/serve_news.py` 的纯离线合成样本计算；不混用席位，不使用未来回放收益筛选股票，不涉及真实证券代码。

## 地图

本地文件 `vendor/countries-110m.json` 来自锁定版本 [world-atlas 2.0.2](https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/countries-110m.json)。这是基于 Natural Earth 的简化国家/陆地拓扑数据（1:110m 比例尺），包含经纬度坐标、国家数字 ID、英文名称与几何边界。数据解释参见 [world-atlas 官方仓库](https://github.com/topojson/world-atlas)。

该小比例尺底图仅用于**地理交互示意，非标准地图**，不保证细小岛屿、边界或名称适合正式地图发布，也不表达主权或法律立场。正式参赛发布前应按投放地区、数据授权和地图展示要求重新审查所选底图。

Natural Earth 地图数据采用公有领域条款，详见 [Natural Earth 使用条款](https://www.naturalearthdata.com/about/terms-of-use/)。world-atlas 软件/分发包附带 ISC 许可，本地保留 `vendor/world-atlas.LICENSE`。

## 固定前端依赖

脚本均为公开包的锁定版本，仅本地载入；未添加 npm 安装、构建工具、遥测、第三方请求或自动更新。

| 资源 | 固定来源 | 本地许可 |
| --- | --- | --- |
| d3-array 3.2.4 | https://cdn.jsdelivr.net/npm/d3-array@3.2.4/dist/d3-array.min.js | `vendor/d3-array.LICENSE` |
| d3-geo 3.1.1 | https://cdn.jsdelivr.net/npm/d3-geo@3.1.1/dist/d3-geo.min.js | `vendor/d3-geo.LICENSE` |
| topojson-client 3.1.0 | https://cdn.jsdelivr.net/npm/topojson-client@3.1.0/dist/topojson-client.min.js | `vendor/topojson-client.LICENSE` |

以上为原始下载文件，无手绘地图边界、无运行时网络回退。载入顺序为 d3-array → d3-geo → topojson-client → 本地预览脚本。

## SHA-256

```text
countries-110m.json  2516c915867c7baf18ddec727aec46c315541a07cfb3d79a6559b05d5e94eee8
d3-array.min.js     80aa70d0cd17dabddf6d056494ea17926a45a69da8b7850220aace331bad671d
d3-geo.min.js       23e574f1e8d4716d622bce45356a379b66a277162b145f5f147e4c8daefb73d2
topojson-client.min.js 25cd02ae486cc5063e0215a4e4cfb15de83700c87ac48bac4d57dc6aaf3ebb89
```

## 运行与检查

从项目目录 `C:\股票数据\contest-edition` 运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 design-preview\news-earth\serve_earth.py --port 3008
.\.venv\Scripts\python.exe -m unittest discover -s design-preview/news-earth -p test_earth_server.py -q
```

HTTP 仅监听 `127.0.0.1`，只接受 GET/HEAD 与精确静态白名单；拒绝外部 Host/Origin、写入方法、请求体和路径遍历。`sphere-core.js` 是一个固定指向相邻旧预览的只读辅助脚本，不开放目录访问。测试使用随机临时端口，不停止已有服务。
