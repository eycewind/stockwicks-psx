#/var/www/stockwicks/app/utils/common/templates.py
def pick_template(base_name: str, user) -> str:
    """
    Return the correct template name for the user.
    If schwab_allowed == 'Y' → return td-* version.
    Otherwise → return normal template.
    """
    if getattr(user, "schwab_allowed", "N") == "Y":
        # example: dashboard.html -> td-dashboard.html
        if base_name == "dashboard.html":
            return "td-dashboard.html"
        elif base_name == "paper_trading_bot.html":
            return "td-paper_trading_bot.html"
    return base_name
