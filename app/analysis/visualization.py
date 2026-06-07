# visualization.py
# app/analysis/visualization.py
from bokeh.plotting import figure
from bokeh.embed import components
from bokeh.models import ColumnDataSource

def plot_stock_data(df):
    source = ColumnDataSource(df)
    p = figure(title="Stock Prices", x_axis_type="datetime")
    p.line('timestamp', 'close', source=source, color='navy', legend_label='Close Price')
    
    script, div = components(p)
    return script, div