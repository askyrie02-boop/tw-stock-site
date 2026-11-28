"""盤中監控：開盤時定時（預設每小時）推送盤中狀況，異常大筆資金進出即時通知。

這個資料夾會跟著網站一起發布到公開的 repo（tw-stock-site），在那裡的 GitHub Actions 執行
（公開 repo 的執行時間不用錢；盤中要連續跑 4.5 小時，私人 repo 的免費額度不夠）。
所以這裡的程式不能用到 backtester 套件，只用 requests、matplotlib 和 Python 內建的功能。
"""
