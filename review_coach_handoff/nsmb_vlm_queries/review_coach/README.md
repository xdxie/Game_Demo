# ReviewCoach 上游输入（马里奥 W1-1）

来源视频：`d:\Desktop\new_super_mario_bros\Video Project 5.mp4`

| 文件 | clip | query 摘要 |
|------|------|------------|
| query_01.json | 12.0-16.0s | 顶问号砖/水管 |
| query_02.json | 30.0-34.0s | 顶砖遇怪 |
| query_03.json | 50.0-52.0s | 云端蘑菇 |
| query_04.json | 41.0-45.0s | 红环红币 |
| query_05.json | 89.0-93.0s | 地下蘑菇怪/坑 |
| query_06.json | 56.0-59.0s | 急着吃金币被怪碰到 |

```powershell
Get-ChildItem "nsmb_vlm_queries\review_coach\query_*.json" | ForEach-Object {
  python run_review_demo.py --input $_.FullName
}
```
