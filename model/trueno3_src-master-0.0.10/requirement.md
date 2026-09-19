1.local_web和=local_inference、run_local.py 是本地pt模型先调用algorithm里调用代码和src\base实现多段推理并用，
但是当前存在一下需求和bug需要处理：
1.1 多端流程无法识别，本质上调用D:\Work\底层原理\trueno3_src-master-0.0.10\src\algorithm内代码和D:\Work\底层原理\trueno3_src-master-0.0.10\src\base代码可以实现多段推理出最后结果
1.2 界面太松散，将UI缩小到不需要下拉滑动即可看到全貌
1.3 分析出得结果图 中文乱码，字体改为本系统可以显示得字体
1.4 日志直接使用src里得日志显示在网页。
