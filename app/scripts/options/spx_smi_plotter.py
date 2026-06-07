import re
from pathlib import Path
import pandas as pd
import matplotlib.pyplot as plt

LOG_PATH = Path('/var/www/stockwicks/data/116/spx0dte_bot.log')
OUT_DIR = Path(__file__).resolve().parent / 'spx_smi_plots'
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Matches log lines like:
# 2026-03-11 09:51:03 [INFO] Latest closed SPX bars:
# ... followed by rows such as:
# 2026-03-11 09:46:00  5578.91  5580.76  5576.34  5579.43
BAR_ROW_RE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+'
    r'(?P<open>-?\d+(?:\.\d+)?)\s+'
    r'(?P<high>-?\d+(?:\.\d+)?)\s+'
    r'(?P<low>-?\d+(?:\.\d+)?)\s+'
    r'(?P<close>-?\d+(?:\.\d+)?)\s*$'
)

# Flexible SMI capture. Handles variations like:
# SMI latest=12.34 signal=10.11 hist=2.23
# SMI=12.34 signal=10.11
# smi_fast=... smi_slow=...
SMI_LINE_RE = re.compile(
    r'(?i)\bSMI\b.*?'
    r'(?:latest|value|main|=)?\s*[:=]?\s*(?P<smi>-?\d+(?:\.\d+)?)'
    r'(?:.*?\bsignal\b\s*[:=]\s*(?P<signal>-?\d+(?:\.\d+)?))?'
    r'(?:.*?\bhist(?:ogram)?\b\s*[:=]\s*(?P<hist>-?\d+(?:\.\d+)?))?'
)

LOG_TS_RE = re.compile(r'^(?P<logts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')


def parse_log(path: Path):
    lines = path.read_text(errors='ignore').splitlines()

    spx_rows = []
    smi_rows = []

    i = 0
    while i < len(lines):
        line = lines[i]

        # Capture SPX raw 1m OHLC tables dumped into the log.
        if 'Latest closed SPX bars' in line or 'closed SPX bars' in line:
            j = i + 1
            while j < len(lines):
                row = lines[j].strip()
                m = BAR_ROW_RE.match(row)
                if not m:
                    break
                spx_rows.append(
                    {
                        'bar_time': pd.to_datetime(m.group('ts')),
                        'open': float(m.group('open')),
                        'high': float(m.group('high')),
                        'low': float(m.group('low')),
                        'close': float(m.group('close')),
                        'source_line': j + 1,
                    }
                )
                j += 1
            i = j
            continue

        # Capture SMI values from log lines.
        if 'SMI' in line.upper():
            tsm = LOG_TS_RE.match(line)
            sm = SMI_LINE_RE.search(line)
            if tsm and sm:
                smi_rows.append(
                    {
                        'log_time': pd.to_datetime(tsm.group('logts')),
                        'smi': float(sm.group('smi')),
                        'signal': float(sm.group('signal')) if sm.group('signal') else None,
                        'hist': float(sm.group('hist')) if sm.group('hist') else None,
                        'raw_line': line.strip(),
                    }
                )

        i += 1

    spx_df = pd.DataFrame(spx_rows)
    smi_df = pd.DataFrame(smi_rows)

    if not spx_df.empty:
        spx_df = (
            spx_df.sort_values('bar_time')
            .drop_duplicates(subset=['bar_time'], keep='last')
            .reset_index(drop=True)
        )

    if not smi_df.empty:
        smi_df['minute'] = smi_df['log_time'].dt.floor('min')
        smi_df = (
            smi_df.sort_values('log_time')
            .drop_duplicates(subset=['minute'], keep='last')
            .reset_index(drop=True)
        )

    return spx_df, smi_df


def plot_spx_and_smi(spx_df: pd.DataFrame, smi_df: pd.DataFrame):
    if spx_df.empty and smi_df.empty:
        raise RuntimeError('No SPX or SMI data could be parsed from the log.')

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(16, 10), sharex=True, gridspec_kw={'height_ratios': [3, 1]}
    )

    if not spx_df.empty:
        ax1.plot(spx_df['bar_time'], spx_df['close'], color='black', linewidth=1.5, label='SPX close')
        ax1.fill_between(spx_df['bar_time'], spx_df['low'], spx_df['high'], color='steelblue', alpha=0.15, label='SPX 1m range')
        ax1.set_ylabel('SPX')
        ax1.set_title('SPX 1-Minute Raw Bars and SMI from Log')
        ax1.grid(True, alpha=0.25)
        ax1.legend(loc='upper left')

    if not smi_df.empty:
        ax2.plot(smi_df['minute'], smi_df['smi'], color='purple', linewidth=1.5, label='SMI')
        if smi_df['signal'].notna().any():
            ax2.plot(smi_df['minute'], smi_df['signal'], color='orange', linewidth=1.2, label='Signal')
        if smi_df['hist'].notna().any():
            colors = ['#16a34a' if x >= 0 else '#dc2626' for x in smi_df['hist'].fillna(0)]
            ax2.bar(smi_df['minute'], smi_df['hist'].fillna(0), width=0.0008, alpha=0.3, color=colors, label='Hist')
        ax2.axhline(0, color='gray', linewidth=1, alpha=0.7)
        ax2.set_ylabel('SMI')
        ax2.grid(True, alpha=0.25)
        ax2.legend(loc='upper left')

    ax2.set_xlabel('Time')
    fig.autofmt_xdate()
    fig.tight_layout()

    out_png = OUT_DIR / 'spx_smi_overlay.png'
    fig.savefig(out_png, dpi=180)
    plt.close(fig)
    return out_png


def main():
    spx_df, smi_df = parse_log(LOG_PATH)

    spx_csv = OUT_DIR / 'spx_raw_1m.csv'
    smi_csv = OUT_DIR / 'smi_raw.csv'

    spx_df.to_csv(spx_csv, index=False)
    smi_df.to_csv(smi_csv, index=False)

    chart_path = plot_spx_and_smi(spx_df, smi_df)

    print(f'SPX rows: {len(spx_df)}')
    print(f'SMI rows: {len(smi_df)}')
    print(f'Saved: {spx_csv}')
    print(f'Saved: {smi_csv}')
    print(f'Saved: {chart_path}')

    if not spx_df.empty:
        print('\nSPX sample:')
        print(spx_df.tail(10).to_string(index=False))

    if not smi_df.empty:
        print('\nSMI sample:')
        print(smi_df.tail(10)[['log_time', 'smi', 'signal', 'hist']].to_string(index=False))


if __name__ == '__main__':
    main()
