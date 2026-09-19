# sources 压缩包重构实现方案

## 目标

把 `sources` 里的外层压缩包解开后，按业务规则把多个内层包重新整合，最后生成一份完整的新压缩包。核心要求是：

1. 外层 ZIP 先解压，再解压其中的内层 ZIP。
2. 若存在多个 `accuracy_数字*.zip` / `image_result_数字*.zip`，按数字对应关系合并，最终只保留一对新的 `accuracy_数字.zip` 和 `image_result_数字.zip`。
3. `id`、`taskID`、时间相关字段、图片命名要同步更新。
4. `projectSN`、`stationSN` 不改。
5. 新增 `config.yaml`，驱动 `accuracy.json` 的排序和图片复制改名。
6. 重打包后输出一份新的完整压缩包，日志写入 `log.txt`。

## 现有样本理解

当前仓库里的样本已经验证过一遍基本结构：

- `accuracy.json` 里有 `data.statistic[]`
- 每个 `statistic` 对应一个 `algoType`
- 每个 `algoType` 下的 `imagesData[]` 和 `image/<algoType>/*.jpg` 一一对应
- `recognition` 是后续排序和改名的核心依据

这份样本是实现脚本的参考基线，但脚本要支持“一个外层包里有多个内层包”的情况，不只处理单一 pair。

## 输入结构

外层包解压后，可能出现多组内层包，例如：

```text
accuracy_1.zip
accuracy_2.zip
accuracy_3.zip
image_result_1.zip
image_result_2.zip
image_result_3.zip
```

处理原则是：

- 同数字的 `accuracy_*` 和 `image_result_*` 先配对。
- 同一外层任务下的多组内容再合并成一份最终结果。
- 最终外层包里只保留一对新的内层 ZIP。

## config.yaml

新增的 `config.yaml` 只负责提供 `recognition` 关键字列表，按文件顺序表示优先级。

示例：

```yaml
keywords:
  - 红灯亮
  - 绿灯亮
  - 灯灭
```

约定：

- 匹配方式用“子字符串包含”。
- 同一条 `recognition` 命中多个关键字时，按 `keywords` 里的先后顺序取第一个命中。
- 这个顺序同时用于排序和筛选复制。

## 处理流程

### 1. 解压与归并

1. 在临时工作目录解压外层 ZIP。
2. 再解压所有内层 ZIP。
3. 识别 `accuracy_*` 和 `image_result_*` 的配对关系。
4. 把同类内容归并到统一工作树里，生成最终的一份 `accuracy.json` 和一份图片树。

### 2. 元数据更新

用结构化解析方式改，不做整文件字符串替换。

需要同步更新的内容包括：

- `id`
- `taskID`
- 所有时间相关字段
- YAML 里涉及任务号和时间的变量

保持不变的内容：

- `projectSN`
- `stationSN`

时间相关字段统一以新执行时间为准，`statisticDate` 也按新日期重算。

### 3. `accuracy.json` 处理

对每个 `algoType` 单独处理：

1. 读取该块下的 `imagesData`。
2. 先按 `config.yaml` 的关键字把命中的项识别出来。
3. 命中的项整体放到该 `algoType` 块的末尾。
4. 块内保持稳定排序，未命中的项保持原始相对顺序。
5. 命中的项里，如果 `recognition` 对应的图片需要重命名，就同步更新 `imagesData.imageName`。

这里的“放到末尾”只改变块内顺序，不改 `recognition` 本身。

### 4. 图片复制与重命名

对命中 `config.yaml` 关键字的图片：

1. 在根目录下创建日期目录 `image_YYYYMMDD/`，原文里写作 `iamge+日期`，这里按规范写成 `image_YYYYMMDD/`。
2. 复制对应图片到这个目录。
3. 用 `recognition + pointName` 作为新文件名。
4. 保留 `.jpg` 扩展名。
5. 文件名里的非法字符先清理。
6. 如果重名，追加序号，避免覆盖。

如果最终业务还要求保留原来的 `image/<algoType>/` 结构，可以用同一份重命名映射同步处理原目录文件。

## 输出规则

1. 重打包时，外层包名沿用原命名模板，只替换尾部业务后缀或编号，不改 `.zip` 扩展名。
2. 内层 ZIP 也按同样规则输出成新的 `accuracy_数字.zip` 和 `image_result_数字.zip`。
3. 最终外层 ZIP 只包含这两个内层 ZIP。
4. 不覆盖原始输入包，所有结果写到新的输出目录。

## 校验

重打包前后都要检查：

- JSON / YAML 都能正常解析
- `taskID`、时间字段一致
- `projectSN`、`stationSN` 未被改动
- 每个 `algoType` 的条目数和图片数一致
- `imageName` 能找到对应图片
- 没有重复名、缺失文件、孤立文件
- 外层 ZIP 里只有预期的两个内层 ZIP

## 日志

执行过程统一写入 `log.txt`，至少记录这些阶段：

- 解压
- 归并
- 读取 `config.yaml`
- 排序
- 复制和改名
- 重打包
- 校验结果

