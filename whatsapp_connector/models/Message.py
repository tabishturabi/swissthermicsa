# -*- coding: utf-8 -*-
import hashlib
import base64
from werkzeug import secure_filename
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError
from ..tools import date2local, date_timedelta
from odoo.tools import formatLang
from odoo.addons.whatsapp_connector.tools import get_binary_attach, date_delta_seconds


class AcruxChatMessages(models.Model):
    _inherit = 'acrux.chat.base.message'
    _name = 'acrux.chat.message'
    _description = 'Chat Message'
    _order = 'date_message desc, id desc'

    name = fields.Char('name', compute='_compute_name', store=True)
    msgid = fields.Char('Message Id')
    contact_id = fields.Many2one('acrux.chat.conversation', 'Contact',
                                 required=True, ondelete='cascade')
    connector_id = fields.Many2one('acrux.chat.connector', related='contact_id.connector_id',
                                   string='Connector', store=True, readonly=True)
    date_message = fields.Datetime('Date', required=True, default=fields.Datetime.now)
    from_me = fields.Boolean('Message From Me')
    company_id = fields.Many2one('res.company', related='contact_id.company_id',
                                 string='Company', store=True, readonly=True)
    ttype = fields.Selection(selection_add=[('contact', 'Contact'),
                                            ('product', 'Product')])
    error_msg = fields.Char('Error Message', readonly=True)
    event = fields.Selection([('unanswered', 'Unanswered Message'),
                              ('new_conv', 'New Conversation'),
                              ('res_conv', 'Resume Conversation')],
                             string='Event')
    user_id = fields.Many2one('res.users', string='Sellman', compute='_compute_user_id',
                              store=True)

    @api.depends('contact_id')
    def _compute_user_id(self):
        for r in self:
            user_id = r._get_user_id()
            r.user_id = user_id or self.env.user.id

    def _get_user_id(self):
        user_id = False
        if self.contact_id.sellman_id:
            user_id = self.contact_id.sellman_id.id
        return user_id

    @api.depends('text')
    def _compute_name(self):
        for r in self:
            if r.text:
                r.name = r.text[:10]
            else:
                r.name = '/'

    def conversation_update_time(self):
        for mess in self:
            is_info = bool(mess.ttype and mess.ttype.startswith('info'))
            if not is_info:
                data = {}
                cont = mess.contact_id
                if mess.from_me:
                    data.update({'last_sent': mess.date_message})
                    if cont.last_received:
                        data.update({'last_received_first': False})
                else:
                    # nº message
                    data.update({'last_received': mess.date_message})
                    # 1º message
                    if not cont.last_received_first:
                        data.update({'last_received_first': mess.date_message})
                if data:
                    cont.write(data)

    @api.model
    def create(self, vals):
        if vals.get('contact_id'):
            Conv = self.env['acrux.chat.conversation']
            conv_id = Conv.browse([vals.get('contact_id')])
            if not conv_id.last_received:
                vals.update(event='new_conv')
            elif conv_id.last_received < date_timedelta(minutes=-12 * 60):
                ''' After 12 hours it is resume '''
                vals.update(event='res_conv')
        ret = super(AcruxChatMessages, self).create(vals)
        ret.conversation_update_time()
        return ret

    @api.model
    def clean_number(self, number):
        return number.replace('+', '').replace(' ', '')

    @api.model
    def unlink_attachment(self, attach_to_del_ids, only_old=True):
        data = [('id', 'in', attach_to_del_ids)]
        if only_old:
            data.append(('delete_old', '=', True))
        to_del = self.env['ir.attachment'].sudo().search(data)
        erased_ids = to_del.ids
        to_del.unlink()
        return erased_ids

    def unlink(self):
        ''' Delete attachment too '''
        mess_ids = self.filtered(lambda x: x.res_model == 'ir.attachment' and x.res_id)
        attach_to_del = mess_ids.mapped('res_id')
        ret = super(AcruxChatMessages, self).unlink()
        if attach_to_del:
            self.unlink_attachment(attach_to_del)
        return ret

    def getJsDitc(self):
        out = self.read(['id', 'text', 'ttype', 'date_message', 'from_me', 'res_model',
                         'res_id', 'error_msg'])
        for x in out:
            x['date_message'] = date2local(self, x['date_message'])
        return out

    @api.model
    def get_url_image(self, res_model, res_id, field='image_chat', prod_id=None):
        url = False
        if not prod_id:
            prod_id = self.env[res_model].search([('id', '=', res_id)], limit=1)
        prod_id = prod_id if len(prod_id) == 1 else False
        if prod_id:
            field_obj = getattr(prod_id, field)
            if not field_obj:
                return prod_id, False
            check_weight = self.message_check_weight(field=field_obj)
            if check_weight:
                hash_id = hashlib.sha1(str((prod_id.write_date or prod_id.create_date or '')).encode('utf-8')).hexdigest()[0:7]
                url = '/web/static/chatresource/%s/%s_%s/%s' % (prod_id._name, prod_id.id, hash_id, field)
                base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
                url = base_url.rstrip('/') + url
        return prod_id, url

    @api.model
    def get_url_attach(self, att_id):
        url = False
        attach_id = self.env['ir.attachment'].sudo().search([('id', '=', att_id)], limit=1)
        attach_id = attach_id if len(attach_id) == 1 else False
        if attach_id:
            self.message_check_weight(value=attach_id.file_size, raise_on=True)
            access_token = attach_id.generate_access_token()[0]
            url = '/web/chatresource/%s/%s' % (attach_id.id, access_token)
            base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
            url = base_url.rstrip('/') + url
        return attach_id, url

    def message_parse(self):
        ''' Return message formated '''
        self.ensure_one()
        message = False
        if self.ttype == 'text':
            message = self.ca_ttype_text()
        elif self.ttype in ['image', 'video', 'file']:
            message = self.ca_ttype_file()
        elif self.ttype == 'audio':
            message = self.ca_ttype_audio()
        elif self.ttype == 'product':
            message = self.ca_ttype_product()
        elif self.ttype == 'sale_order':
            message = self.ca_ttype_sale()
        elif self.ttype == 'location':
            message = self.ca_ttype_location()
        elif self.ttype == 'contact':
            raise ValidationError('Not implemented')
        message.update({
            'to': self.clean_number(self.contact_id.number),
            'id': str(self.id),
        })
        return message

    def message_send(self):
        '''Return msgid
        In: {'type': string (required) ['text', 'image', 'video', 'file', 'audio', 'location'],
             'text': string (required),
             'from': string,
             'to': string,
             'filename': string,
             'url': string,
             'address': string,
             'latitude': string,
             'longitude': string,
             }
        Out: {'msg_id': [string, False],
              }
        '''
        self.ensure_one()
        ret = False
        connector_id = self.contact_id.connector_id
        if not self.ttype.startswith('info'):
            self.message_check_allow_send()
            data = self.message_parse() or {}
            result = connector_id.ca_request('send', data)
            msg_id = result.get('msg_id', False)
            if msg_id:
                self.msgid = msg_id
                return msg_id
            else:
                raise ValidationError('Server error.')
        else:
            return ret

    def message_check_allow_send(self):
        ''' Check elapsed time '''
        self.ensure_one()
        if self.text and len(self.text) >= 4000:
            raise ValidationError(_('Message is to large (4.000 caracters).'))
        connector_id = self.contact_id.connector_id
        if connector_id.connector_type == 'gupshup':
            last_received = self.contact_id.last_received
            max_hours = connector_id.time_to_respond
            if max_hours and max_hours > 0:
                if not last_received:
                    raise ValidationError(_('The client must have started a conversation.'))
                diff_hours = date_delta_seconds(last_received) / 3600
                if diff_hours >= max_hours:
                    raise ValidationError(_('The time to respond exceeded (%s hours). '
                                          'The limit is %s hours.') % (int(round(diff_hours)), max_hours))

    def message_check_weight(self, field=None, value=None, raise_on=False):
        ''' Check size '''
        self.ensure_one()
        ret = True
        limit = int(self.env['ir.config_parameter'].sudo().get_param('acrux_max_weight_kb') or '0')
        if limit > 0:
            limit *= 1024  # el parametro esta en kb pero el value pasa en bytes
            if field:
                value = len(base64.b64decode(field) if field else b'')
            if (value or 0) >= limit:
                if raise_on:
                    msg = '%s Kb' % limit if limit < 1000 else '%s Mb' % (limit / 1000)
                    raise ValidationError(_('Attachment exceeds the maximum size allowed (%s).') % msg)
                return False
        return ret

    @api.model
    def ca_ttype_text(self):
        ret = {
            'type': 'text',
            'text': self.text
        }
        return ret

    @api.model
    def ca_ttype_audio(self):
        if not self.res_id or self.res_model != 'ir.attachment':
            raise ValidationError('Attachment type is required.')
        attach_id, url = self.get_url_attach(self.res_id)
        if not attach_id:
            raise ValidationError('Attachment is required.')
        if not url:
            raise ValidationError('URL Attachment is required.')
        ret = {
            'type': 'audio',
            'url': url
        }
        return ret

    @api.model
    def ca_ttype_file(self):
        if not self.res_id or self.res_model != 'ir.attachment':
            raise ValidationError('Attachment type is required.')
        attach_id, url = self.get_url_attach(self.res_id)
        if not attach_id:
            raise ValidationError('Attachment is required.')
        if not url:
            raise ValidationError('URL Attachment is required.')
        ret = {
            'type': self.ttype,
            'text': self.text or '',
            'filename': attach_id.name,
            'url': url
        }
        return ret

    @api.model
    def ca_ttype_product(self):
        url = False
        filename = ''
        image_field = 'image_chat'  # to set dynamic: self.res_filed
        if not self.res_id or self.res_model != 'product.product':
            raise ValidationError('Product type is required.')
        prod_id = self.env[self.res_model].browse(self.res_id)
        if not prod_id:
            raise ValidationError('Product is required.')

        # caption ----------
        # or prod_id.name_get()[0][1]
        list_price = formatLang(self.env, prod_id.list_price, currency_obj=self.env.user.company_id.currency_id)
        caption = '%s\n%s / %s' % (self.text or prod_id.name.strip(),
                                   list_price, prod_id.uom_id.name)

        # image ----------
        field_image = getattr(prod_id, image_field)
        if field_image:
            filename = secure_filename(prod_id.name)
            attach = get_binary_attach(self.env, self.res_model, self.res_id, image_field,
                                       fields_ret=['mimetype'])
            mimetype = attach and attach['mimetype']
            if mimetype:
                ext = mimetype.split('/')
                if len(ext) == 2:
                    filename = secure_filename('%s.%s' % (prod_id.name, ext[1]))

            prod_id, url = self.get_url_image(res_model=self.res_model, res_id=self.res_id,
                                              field=image_field, prod_id=prod_id)
        # send ----------
        if not url:
            # Simple text message
            ret = {
                'type': 'text',
                'text': caption
            }
            return ret
        else:
            ret = {
                'type': 'file',
                'text': caption,
                'filename': filename,
                'url': url
            }
        return ret

    @api.model
    def ca_ttype_sale(self):
        if self.res_model != 'sale.order':
            raise ValidationError('Order type is required.')
        return self.ca_ttype_file()

    @api.model
    def ca_ttype_location(self):
        ''' Text format:
                name
                address
                latitude, longitude
        '''
        parse = self.text.split('\n')
        if len(parse) != 3:
            return self.ca_ttype_text()
        cords = parse[2].split(',')
        ret = {
            'type': 'location',
            'address': '%s\n%s' % (parse[0].strip(), parse[1].strip()),
            'latitude': cords[0].strip('( '),
            'longitude': cords[1].strip(') '),
        }
        return ret
