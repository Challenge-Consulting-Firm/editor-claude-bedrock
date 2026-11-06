# PoC 中の暴走防止（Azure 版 R3 のソフトリミット相当）。
# 50/75/90% 実績 + 100% 予測でメール通知。
# ハードストップ（Azure 版 disableLocalAuth 相当 = キー無効化）は本番化スコープ（docs/design.md §7）。

resource "aws_budgets_budget" "monthly" {
  name         = "editor-claude-bedrock-poc"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "Service"
    values = ["Amazon Bedrock"]
  }

  dynamic "notification" {
    for_each = [50, 75, 90]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.ops_email]
    }
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.ops_email]
  }
}
