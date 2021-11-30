# -*- coding: utf-8 -*-
import logging
import unicodedata
import json
import werkzeug
import base64
from odoo import http, _, fields
from odoo.http import request, Response
from odoo.addons.web.controllers.main import serialize_exception
from odoo.exceptions import UserError, QWebException
from psycopg2 import OperationalError
from psycopg2.extensions import TransactionRollbackError
from datetime import datetime
from odoo.addons.whatsapp_connector.tools import get_image_from_url
_logger = logging.getLogger(__name__)


class WebhookController(http.Controller):

    @http.route('/acrux_webhook/whatsapp_connector/<string:connector_uuid>', auth='public', type='json', method=['POST'])
    def acrux_webhook(self, connector_uuid, **post):
        ''' Keeping URLs secret. '''
        '''
            connector_id: viene en la URL
            Json: {'messages': [{'ttype': string ['text', 'image', 'audio', 'video', 'file'],
                                 'name': string (sender name),
                                 'number': string,
                                 'txt': string,
                                 'url': string,
                                 'time': timestamp},
                               ],
                   'updates': [{'number': string (para buscar el contacto),
                               'name': string (enviar algo si se cambia nombre),
                               'image_url': string (enviar algo si se cambia avatar)},
                             ]
                   'events': # gupshup -> 'message-event' - chatapi -> 'ack'
                            [{'type': string ['failed'] (solo tenemos implementado para gupshup 'failed'),
                              'msgid': string (para buscar el mensaje),
                              'txt': string (mensaje)},
                            ]
            return: status = 200, 403 ó 500 (Supongo el raise vuelve a intentar y pero no se que devuelve al final...)
            '''
        try:
            body = request.jsonrequest
            if not body:
                return Response(status=403)  # Forbidden
            _logger.info('\n%s' % json.dumps(body, indent=2))
            body = body['params']

            updates = body.get('updates', [])
            events = body.get('events', [])
            messages = body.get('messages', [])
            if not updates and not events and not messages:
                return Response(status=403)  # Forbidden

            Conversation = request.env['acrux.chat.conversation'].sudo()
            Connector = request.env['acrux.chat.connector'].sudo()
            connector_id = Connector.search([('uuid', '=', connector_uuid)], limit=1)
            if not connector_id or not connector_uuid:
                return Response(status=403)  # Forbidden

            for contact in updates:
                number = '+' + contact.get('number')
                name = contact.get('name') or ''
                image_url = contact.get('image_url') or ''
                if name or image_url:
                    conv_id = Conversation.search([('number', '=', number),
                                                   ('connector_id', '=', connector_id.id)])
                    if conv_id:
                        if image_url and image_url.startswith('http'):
                            raw_image = get_image_from_url(image_url)
                            conv_id.image_128 = raw_image
                        if name:
                            conv_id.name = name

            for event in events:
                ttype = event.get('type')
                # OJO: solo analizaremos failed
                if ttype == 'failed':
                    msgid = event.get('msgid')
                    reason = event.get('txt')
                    if msgid and reason:
                        Conversation.new_message_event(connector_id, msgid, reason)
                    _logger.warning(event)

            for mess in messages:
                ttype = mess.get('type')
                text = mess.get('txt')
                if ttype not in ['text', 'image', 'audio', 'video', 'file', 'location']:
                    ttype = 'text'
                    text = text or 'Message type Not allowed (%s).' % ttype
                data = {
                    'ttype': ttype,
                    'connector_id': connector_id.id,
                    'name': mess.get('name'),
                    'number': '+' + mess.get('number'),
                    'message': text,
                    'url': mess.get('url'),
                    'time': datetime.fromtimestamp(mess.get('time')) if mess.get('time') else fields.Datetime.now()
                }
                Conversation.new_message(data)
            return Response(status=200)
        except (TransactionRollbackError, OperationalError, QWebException) as e:
            raise e
        except Exception:
            request._cr.rollback()
            _logger.error('Error', exc_info=True)
            return Response(status=500)  # Internal Server Error

    @http.route('/whatsapp_connector/new_message', type='http', auth='user',
                method=['OPTIONS', 'POST'], csrf=False)
    def new_message(self, **params):
        AcruxChatConnector = request.env['acrux.chat.connector'].sudo()
        AcruxChatConversation = request.env['acrux.chat.conversation'].sudo()
        params['connector_id'] = AcruxChatConnector.search([], limit=1).id
        params['ttype'] = 'text'
        AcruxChatConversation.new_message(params)
        return 'prueba'

    def chek_error(self, status, content, headers):
        if status == 304:
            return Response(status=304, headers=headers)
        elif status == 301:
            return werkzeug.utils.redirect(content, code=301)
        if not content:
            return Response(status=404)

    @http.route('/web/chatresource/<int:res_id>', type='http', auth='user')
    def acrux_web_content_login(self, res_id):
        status, headers, content = request.env['ir.http'].sudo().binary_content(model='ir.attachment',
                                                                                id=res_id, field='datas')
        error = self.chek_error(status, content, headers)
        if error:
            return error
        content_b64 = base64.b64decode(content)
        headers.append(('Content-Length', len(content_b64)))
        headers.append(('Accept-Ranges', 'bytes'))
        response = request.make_response(content_b64, headers)
        response.status_code = status
        return response

    @http.route(['/web/chatresource/<int:id>/<string:access_token>',
                 '/web/static/chatresource/<string:model>/<string:id>/<string:field>'],
                type='http', auth='public', sitemap=False)
    def acrux_web_content(self, id=None, model=None, field=None, access_token=None):
        '''
        /web/chatresource/...        -> for attachment
        /web/static/chatresource/... -> for product image
        :param field: field (binary image, PNG or JPG) name in model. Only support 'image'.
        '''

        if id and access_token and not model and not field:
            status, headers, content = request.env['ir.http'].sudo().binary_content(model='ir.attachment',
                                                                                    id=int(id), field='datas',
                                                                                    access_token=access_token)
            error = self.chek_error(status, content, headers)
            if error:
                return error
            content_b64 = base64.b64decode(content)
        else:
            if not id or not field.startswith('image') or model not in ['product.template', 'product.product']:
                return Response(status=404)

            id, sep, unique = id.partition('_')
            status, headers, content = request.env['ir.http'].sudo().binary_content(model=model, id=int(id),
                                                                                    field=field, unique=unique)
            error = self.chek_error(status, content, headers)
            if error:
                return error
            content_b64 = base64.b64decode(content)

        headers.append(('Content-Length', len(content_b64)))
        response = request.make_response(content_b64, headers)
        response.status_code = status
        return response


class Binary(http.Controller):

    @http.route('/web/binary/upload_attachment_chat', type='http', auth="user")
    @serialize_exception
    def upload_attachment_chat(self, callback, model, id, ufile):
        ''' Source: web.controllers.main.Binary.upload_attachment '''
        files = request.httprequest.files.getlist('ufile')
        Model = request.env['ir.attachment']
        out = """<script language="javascript" type="text/javascript">
                    var win = window.top.window;
                    win.jQuery(win).trigger(%s, %s);
                </script>"""
        args = []
        for ufile in files:
            datas = ufile.read()
            filename = ufile.filename
            if request.httprequest.user_agent.browser == 'safari':
                # Safari sends NFD UTF-8 (where é is composed by 'e' and [accent])
                # we need to send it the same stuff, otherwise it'll fail
                filename = unicodedata.normalize('NFD', ufile.filename)

            try:
                if len(datas) > 2000000:
                    raise UserError(_('Too big, max. %s (%s)') % ('2 Mb', filename))
                attachment = Model.create({
                    'delete_old': True,
                    'name': filename,
                    'datas': base64.encodestring(datas),
                    'store_fname': filename,
                    'res_model': 'acrux.chat.message',
                    'res_id': 0
                })
                attachment.generate_access_token()
            except UserError as e:
                args.append({'error': e.args[0]})
                _logger.exception("Fail to upload attachment %s" % ufile.filename)
            except Exception:
                args.append({'error': _("Something horrible happened")})
                _logger.exception("Fail to upload attachment %s" % ufile.filename)
            else:
                args.append({
                    'filename': filename,
                    'mimetype': ufile.content_type,
                    'id': attachment.id
                })
        return out % (json.dumps(callback), json.dumps(args))
