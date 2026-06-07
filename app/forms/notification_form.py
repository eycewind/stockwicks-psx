# app/forms/notification_form.py
# app/forms/notification_form.py

from wtforms import Form, StringField, SelectField, SubmitField
from wtforms.validators import DataRequired

class NotificationForm(Form):  # ✅ Use WTForms base class
    symbol = StringField('Symbol', validators=[DataRequired()])
    interval = SelectField(
        'Interval',
        choices=[
            ('1min', 'Ultra-Scalping-1min'),
            ('3min', 'Micro-Scalping-3min'),
            ('5min', 'Daytrade-Scalping-5min'),
            ('15min', 'Daytrade-Swing-15min'),
            ('60min', 'Multiday-Swing-60min'),
            ('1D', 'Trade-LongTerm-1Day')
        ],
        validators=[DataRequired()]
    )
    submit = SubmitField('Setup Notification')
