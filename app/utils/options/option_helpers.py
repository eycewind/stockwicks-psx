# app/utils/option_helpers.py

def mark_option(opt):
    """Return the mid-price of an option as the mark."""
    if not opt:
        return 0.0
    return opt.get("mid") or round((opt.get("bid", 0.0) + opt.get("ask", 0.0)) / 2.0, 2)

def mark_vertical(short, long):
    """Return the net mark price for a vertical spread."""
    return round(mark_option(short) - mark_option(long), 2)
