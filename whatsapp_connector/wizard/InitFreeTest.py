# -*- coding: utf-8 -*-

from odoo import models, fields
from odoo.exceptions import ValidationError
from odoo.tools.translate import _
from ..tools import phone_format


class InitFreeTestWizard(models.TransientModel):
    _name = 'init.free.test.wizard'
    _description = 'Init Free Test'

    connector_id = fields.Many2one('acrux.chat.connector', 'Connector', required=True,
                                   ondelete='cascade')
    phones = fields.Char('Phones comma separated', required=True,
                         help='Set all number phones comma separated that you want to test, ' +
                         'numbers must include country code example: ' +
                         '56966324806, 584124540202, 59912567845')

    def init_test(self):
        self.ensure_one()
        msg = False
        if self.phones:
            phones = self.phones.strip()
            if phones and phones != '*':
                phones = phones.split(',')
                phones = list(map(lambda x: phone_format(x.strip()).lstrip('+'), phones))
                self.connector_id.init_free_test(phones)
                PopMessage = self.env['acrux.chat.pop.message']
                pop = _('<p>Write messages to %s from your numbers (%s) to start conversations on Chatroom.</p>')
                return PopMessage.message(_('Good !'), pop % (self.connector_id.source, self.phones))
            else:
                msg = _('Phones is required')
        else:
            msg = _('Phones is required')

        if msg:
            raise ValidationError(msg)
