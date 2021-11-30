# -*- coding: utf-8 -*-

import logging
import json
import sys
import requests
from datetime import datetime, timedelta
from odoo import models, fields, api, _
from odoo.tools import DEFAULT_SERVER_DATE_FORMAT
from odoo.http import request
from odoo.exceptions import ValidationError, UserError
from odoo.addons.whatsapp_connector.tools import TIMEOUT, log_request_error, get_image_from_url
_logger = logging.getLogger(__name__)


class AcruxChatConnector(models.Model):
    _name = 'acrux.chat.connector'
    _description = 'Connector Definition'
    _order = 'sequence, id'

    name = fields.Char('Name', required=True)
    sequence = fields.Integer('Priority', required=True, default=1)
    message = fields.Html('Message', readonly=True, default='<i>Important information about the status of your '
                                                            'account will be displayed here.<br/>This value is '
                                                            'updated every time you press the "Check Status" '
                                                            'button.</i>')
    connector_type = fields.Selection([('not_set', 'Not set'),
                                       ('chatapi', 'ChatApi'),
                                       ('gupshup', 'GupShup')],
                                      string='Connect to', default='not_set', required=True,
                                      help='Third-party connector type.')
    company_id = fields.Many2one('res.company', string='Company', required=True,
                                 default=lambda self: self.env.user.company_id)
    team_id = fields.Many2one('crm.team', string='Team',
                              domain="[('company_id', 'in', [company_id, False])]",
                              ondelete='set null')
    verify = fields.Boolean('Verify SSL', default=True, help='Set False if SSLError: bad handshake - ' +
                                                             'certificate verify failed.')
    source = fields.Char('Phone (Whatsapp)', required=True, default='/',
                         help='Whatsapp phone number.\nThis value is established when hiring '
                              'a plan at www.acruxlab.com.')
    endpoint = fields.Char('API Endpoint', required=True, default='https://api.acruxlab.net/prod/v1/odoo',
                           help='API Url. Please don\'t change.')
    token = fields.Char('Token', required=True, copy=False)
    uuid = fields.Char('Account ID', required=True, copy=False)
    time_to_respond = fields.Integer('Time to Respond (Hours)', default=23,
                                     help='Expiry time in hours to respond message without additional fee.\n' +
                                     'Null or 0 indicate no limit.')
    time_to_reasign = fields.Integer('Time to reasign (Minutes)', default=10,
                                     help='Time in which the conversation is released to be taken by another user.')
    border_color = fields.Char(string="Border Color", size=7, default="#FFFFFF", required=True,
                               help="Border color to differentiate conversation connector")
    ca_status = fields.Boolean('Connected', default=False)
    ca_status_txt = fields.Char('Status')
    ca_qr_code = fields.Binary('QR Code')
    environment = fields.Selection([('test', 'Test'),
                                    ('prod', 'Production')], 'Environment')

    _sql_constraints = [
        ('name_uniq', 'unique (name)', _('Name must be unique.')),
        ('uuid_uniq', 'unique (uuid)', _('Identifier must be unique.')),
    ]

    @api.model
    def default_get(self, fields):
        vals = super(AcruxChatConnector, self).default_get(fields)
        domain = [('company_id', 'in', [self.env.company.id, False])]
        vals['team_id'] = self.env['crm.team'].search(domain, limit=1).id
        return vals

    @api.onchange('company_id')
    def _onchange_company_id(self):
        if self.team_id.company_id.id != self.company_id.id:
            self.team_id = False

    @api.constrains('border_color')
    def constrains_border_color(self):
        for r in self:
            if r.border_color != '#FFFFFF':
                if self.search_count([('border_color', '=', r.border_color)]) > 1:
                    raise ValidationError(_('Color must be unique per connector.'))

    def del_and_recreate_image_chat(self):
        Product = self.env['product.product'].sudo()
        prod_ids = Product.search([('image_chat', '!=', False)])
        prod_ids.write({'image_chat': False})
        Product._recreate_image_chat()

    @api.model
    def execute_maintenance(self, days=21):
        ''' Call from cron.
            Delete attachment older than N days. '''
        Message = self.env['acrux.chat.message']
        date_old = datetime.now() - timedelta(days=int(days))
        date_old = date_old.strftime(DEFAULT_SERVER_DATE_FORMAT)
        mess_ids = Message.search([('res_model', '=', 'ir.attachment'),
                                   ('res_id', '!=', False),
                                   ('date_message', '<', date_old)])
        attach_to_del = mess_ids.mapped('res_id')
        erased_ids = Message.unlink_attachment(attach_to_del)
        for mess_id in mess_ids:
            if mess_id.res_id in erased_ids:
                text = '%s\n(Attachment removed)' % mess_id.text
                mess_ids.write({'text': text.strip(),
                                'res_id': False})
        _logger.info('________ | execute_maintenance: Deleting %s attachments older than %s' %
                     (len(attach_to_del), date_old))

    def _get_custom_info(self):
        self.ensure_one()
        cp = self.company_id
        return {
            'lang': cp.partner_id.lang,
            'phone': cp.phone,
            'vat': cp.vat,
            'currency': cp.currency_id.name,
            'country': cp.country_id.name,
            'name': cp.name,
            'email': cp.email,
        }

    def ca_set_settings(self):
        self.ensure_one()
        param = {'info': self._get_custom_info()}
        self.ca_request('config_set', param)

    def ca_get_chat_list(self):
        # API: retorna {'dialogs': []} -> lo mismo que pasa chat-api!
        # API: si gupshup devolver code = 204
        self.ensure_one()
        data = self.ca_request('contact_get_all')
        dialogs = data.get('dialogs', [])
        vals = {}
        for user in dialogs:
            phone = '+' + user.get('id', '').split('@')[0]
            name = user.get('name', '')
            image_url = user.get('image', '')
            vals[phone] = {'name': name, 'image_url': image_url}
        Conversation = self.env['acrux.chat.conversation']
        ''' Search in conversations of all connector ! '''
        for conv in Conversation.search([('image_128', '=', False)]):
            if conv.number in vals:
                image_url = vals[conv.number].get('image_url', '')
                if image_url and image_url.startswith('http'):
                    raw_image = get_image_from_url(image_url)
                    conv.image_128 = raw_image

    def ca_set_logout(self):
        # API: si gupshup devolver code = 204
        self.ensure_one()
        self.ca_request('status_logout', timeout=20)
        self.ca_status = False
        self.ca_qr_code = False
        message = 'Wait a minute and try to connect.'
        return self.env['acrux.chat.pop.message'].message('Send Logout', message)

    def ca_get_status(self):
        ''' API: {'odoo_url': odoo_url,
                  'source': string,
                  'connector_type': string,
                  'environment': 'prod o test_xxx'
                  'status': {'acrux_ok': 'texto a mostrar'
                             ó 'acrux_er': 'texto a mostrar'
                             ó dict de chatapi}
                 }
        '''
        self.ensure_one()
        Pop = self.env['acrux.chat.pop.message']
        message = detail = False
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        data = self.ca_request('status_get', timeout=20)

        self.source = data.get('source', '*****')
        self.connector_type = data.get('connector_type', '*')
        environment = data.get('environment')
        if environment and environment.startswith('test'):
            environment = 'test'
        self.environment = environment
        odoo_url = data.get('odoo_url', '*')
        if base_url.rstrip('/') != odoo_url.rstrip('/'):
            self.ca_status = False
            self.message = 'Config Error.'
            message = 'URLs are not the same!<br/>Odoo URL:%s<br/>Config URL %s' % (base_url, odoo_url)
            return Pop.message('Error', message)

        status = data.get('status')
        acrux_ok = status.get('acrux_ok')
        acrux_er = status.get('acrux_er')
        accountStatus = status.get('accountStatus')
        if acrux_ok:
            self.ca_status = True
            self.message = acrux_ok
            message = 'Status'
            detail = acrux_ok
            self.ca_set_settings()
        elif acrux_er:
            self.ca_status = False
            self.message = acrux_er
            message = 'Status'
            detail = acrux_er
        elif accountStatus:
            qrCode = status.get('qrCode')
            if accountStatus == 'authenticated':
                self.ca_status = True
                self.ca_qr_code = False
                self.message = False
                message = 'All good!'
                detail = 'WhatsApp connects to your phone to sync messages. ' \
                         'To reduce data usage, connect your phone to Wi-Fi.'
                self.ca_set_settings()
            elif accountStatus == 'got qr code':
                self.ca_status = False
                if qrCode:
                    self.ca_qr_code = qrCode.split('base64,')[1]
                    self.message = 'Please Scan QR code'
                else:
                    self.ca_qr_code = False
                    message = 'An unexpected error occurred. Please try again.'
                    self.message = message
            else:
                self.ca_status = False
                self.message = 'An unexpected error occurred. Please try again.'
                statusData = status.get('statusData')
                title = statusData.get('title')
                msg = statusData.get('msg')
                substatus = statusData.get('substatus')
                message = 'Status: %s' % (substatus or '-')
                detail = '<b>%s</b><br/>%s' % (title, msg)
        return Pop.message(message, detail) if message else True

    def ca_request(self, path, param={}, timeout=False):
        ''' Estado respuesta:
                200		Ok (el resto hace raise)
                202		Accepted (error en el proveedor o cuenta impaga)
                204		No Content (método o parámetro no implementado para este conector)
                400		Bad request. Please pass a valid value in the parameters.
                403		Forbidden. Invalid authentication.
                404		Not found.
                500		Internal server error. (error en lambda)
        '''
        def response_handle_error(req):
            error = False
            try:
                ret = req.json()
            except ValueError as _e:
                ret = {}
            if req.status_code == 200:
                return ret
            elif req.status_code == 202:
                error = ret.get('error', '3rd party connector error. Please try again or check configuration.')
            elif req.status_code == 204:
                error = ret.get('error', '3rd party connector not implement this option.')
            elif req.status_code == 400:
                error = ret.get('error', 'Bad request. Please pass a valid value in the parameters.')
            elif req.status_code == 403:
                error = ret.get('error', 'Forbidden. Invalid authentication.')
            elif req.status_code == 404:
                error = ret.get('error', 'Connector URL not found. Please set correctly.')
            elif req.status_code == 500:
                error = ret.get('error', 'Internal server error. Please try again.')
            else:
                error = ret.get('error', 'Unknown error.')
            log_request_error([error, req_type, path, param], req)
            raise ValidationError(error)

        self.ensure_one()
        actions = {'send': 'post',
                   'msg_set_read': 'get',
                   'config_get': 'get',
                   'config_set': 'post',
                   'status_get': 'get',
                   'status_logout': 'get',
                   'contact_get': 'get',
                   'contact_get_all': 'get',
                   'init_free_test': 'post'}
        req_type = actions[path]
        result = {}
        timeout = timeout or TIMEOUT
        url = self.endpoint.strip('/')
        header = {'Accept': 'application/json',
                  'token': self.token,
                  'client_id': self.uuid,
                  'action': path}
        req = False
        try:
            print('==> REQUEST %4s TO: %s' % (req_type, url))
            if param:
                print('==> DATA: %s' % json.dumps(param, indent=1))
            if req_type == 'post':
                data = json.dumps(param)
                header.update({'Content-Type': 'application/json'})
                req = requests.post(url, data=data, headers=header, timeout=timeout, verify=self.verify)
                result = response_handle_error(req)
            elif req_type == 'get':
                req = requests.get(url, params=param, headers=header, timeout=timeout, verify=self.verify)
                result = response_handle_error(req)
        except requests.exceptions.SSLError as _err:
            log_request_error(['SSLError', req_type, path, param])
            raise UserError(_('Error! Could not connect to Chat-Api server. '
                              'Please in the connector settings, set the '
                              'parameter "Verify" to false by unchecking it and try again.'))
        except requests.exceptions.ConnectTimeout as _err:
            log_request_error(['ConnectTimeout', req_type, path, param])
            raise UserError(_('Timeout error. Try again...'))
        except (requests.exceptions.HTTPError,
                requests.exceptions.RequestException,
                requests.exceptions.ConnectionError) as _err:
            log_request_error(['requests', req_type, path, param])
            ex_type, _ex_value, _ex_traceback = sys.exc_info()
            raise UserError(_('Could not connect to your account.\nPlease check API Endpoint Url.\n%s') % ex_type)
        self.print_result(req_type, url, result, param, req)
        return result

    def print_result(self, req_type, url, result, param, req):
        Host = request.httprequest.headers.get('Host')
        if Host.startswith('localhost'):
            print('status =', req and req.status_code or 'except request')
            # print(request.httprequest.headers)
            print('%%%% => %s %s' % (req_type.upper(), url))
            if param:
                body = param.get('body', False)
                if body:
                    param['body'] = body[0:100]
                data = json.dumps(param, indent=2, sort_keys=True)
                data = data.replace('\\"', "'")
                print(data)
            print('################ resultado')
            data = json.dumps(result, indent=2, sort_keys=True)
            print(data)

    def init_free_test(self, phones):
        self.ensure_one()
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        data = self._get_custom_info()
        data.update({
            'odoo_url': base_url.rstrip('/'),
            'phones': ', '.join(phones),
            'tz': self.env.user.tz,
        })
        result = self.ca_request('init_free_test', data)
        self.source = result.get('source', '*****')
        self.connector_type = result.get('connector_type', '*')
        if result.get('token'):
            self.token = result.get('token')
        if result.get('uuid'):
            self.uuid = result.get('uuid')
        self.ca_status = True
        self.ca_qr_code = False
        self.message = False
        self.environment = 'test'

    def init_free_test_wizard(self):
        self.ensure_one()
        return {
            'name': _('Init Free Test'),
            'type': 'ir.actions.act_window',
            'view_mode': 'form',
            'res_model': 'init.free.test.wizard',
            'target': 'new',
            'context': dict(default_connector_id=self.id)
        }
