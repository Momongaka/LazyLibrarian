#  This file is part of Lazylibrarian.
#  Lazylibrarian is free software':'you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#  Lazylibrarian is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#  You should have received a copy of the GNU General Public License
#  along with Lazylibrarian.  If not, see <http://www.gnu.org/licenses/>.

#  basic opf editor, only edit "our" fields, leave everything else unchanged

import logging
import os
from html import escape  # python 3.x


def opf_read(filename):
    logger = logging.getLogger(__name__)
    matchlogger = logging.getLogger('special.matching')
    if not os.path.exists(filename):
        return '', []
    keys = ['<dc:title>', '<dc:language>', '<dc:publisher>', '<dc:date>']
    subject = '<dc:subject>'
    scheme = '<dc:identifier'
    creator = '<dc:creator'
    content = '<meta content="'
    description = '<dc:description>'
    enddesc = description.replace('<', '</')
    desc = ''
    subjects = 0
    creators = 0
    replaces = []
    outfile = f"{filename}_mod"
    with open(filename) as i:
        data = i.readlines()
    with open(outfile, 'w') as f:
        try:
            for lyne in data:
                if description in lyne:
                    desc = lyne
                elif desc:
                    desc += lyne
                if enddesc in lyne:
                    new_lyne, token, value = extract_key(description, desc)
                    replaces.append((token, value))
                    f.write(new_lyne)
                    desc = ''
                elif not desc:
                    new_lyne = ''
                    for key in keys:
                        if key in lyne:
                            new_lyne, token, value = extract_key(key, lyne)
                            replaces.append((token, value))
                            break

                    if subject in lyne:
                        new_lyne, token, value = extract_key(subject, lyne)
                        indexed = f"subject_{subjects:02d}"
                        subjects += 1
                        new_lyne = new_lyne.replace('$$subject$$', f'$${indexed}$$')
                        replaces.append((indexed, value))

                    if scheme in lyne:
                        new_lyne, token, value = extract_scheme(lyne)
                        replaces.append((token, value))

                    if creator in lyne:
                        indexed = f"_{creators:02d}"
                        creators += 1
                        new_lyne, role, fileas, value = extract_creator(lyne)
                        replaces.append((f"role{indexed}", role))
                        replaces.append((f"creator{indexed}", value))
                        found = False
                        for item in replaces:
                            if item[0] == 'fileas':
                                found = True
                                break
                        if not found:
                            replaces.append(('fileas', fileas))
                        new_lyne = new_lyne.replace('$$role$$', f'$$role{indexed}$$').replace(
                                                    '$$creator$$', f'$$creator{indexed}$$')

                    if content in lyne:
                        new_lyne, token, value = extract_content(content, lyne)
                        replaces.append((token, value))

                    if not new_lyne:
                        new_lyne = lyne
                    f.write(new_lyne)
            items = []
            for item in replaces:
                items.append(item[0])
            matchlogger.debug(','.join(items))
            return outfile, replaces

        except Exception as e:
            logger.debug(str(e))
            return outfile, replaces


def extract_key(key, data):
    ident = key.split(':', 1)[1].split('>')[0]
    endkey = key.replace('<', '</')
    value = data.split(key, 1)[1].split(endkey, 1)[0]
    return data.replace(key + value + endkey, f"{key}$${ident}$${endkey}"), ident, value


def extract_scheme(data):
    ident = data.split('scheme="', 1)[1].split('"')[0]
    value = data.split('>', 1)[1].split('<')[0]
    return data.replace(f">{value}<", f'>$${ident}$$<'), ident, value


def extract_content(key, data):
    ident = data.split('name="', 1)[1].split('"')[0]
    value = data.split(key, 1)[1].split('"')[0]
    return data.replace(f"{key + value}\"", f"{key}$${ident}$$\""), ident, value


def extract_creator(data):
    fileas = data.split('file-as="', 1)[1].split('"')[0]
    role = data.split('role="', 1)[1].split('"')[0]
    value = data.split('>', 1)[1].split('<')[0]
    return data.replace(f">{value}<", '>$$creator$$<').replace(
        f"role=\"{role}\"", 'role="$$role$$"').replace(
        f"file-as=\"{fileas}\"", 'file-as="$$fileas$$"'), role, fileas, value


def opf_write(filename, replaces):
    if not os.path.exists(filename):
        return ''
    with open(filename) as i:
        data = i.readlines()
        outfile = f"{filename}_mod"
        with open(outfile, 'w') as f:
            for lyne in data:
                for item in replaces:
                    lyne = lyne.replace(f'$${item[0]}$$', escape(item[1]))
                f.write(lyne)
    return outfile
