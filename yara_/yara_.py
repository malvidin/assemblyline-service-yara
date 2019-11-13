import json
import os
import threading
from typing import List

import glob
import yara

from assemblyline.common import forge
from assemblyline.common.str_utils import safe_str
from assemblyline_v4_service.common.base import ServiceBase
from assemblyline_v4_service.common.result import Result, ResultSection, BODY_FORMAT

Classification = forge.get_classification()
FILE_UPDATE_DIRECTORY = os.environ.get('FILE_UPDATE_DIRECTORY')


class YaraMetadata(object):
    def __init__(self, match):
        meta = match.meta
        self.name = match.rule
        self.id = meta.get('id', meta.get('rule_id', meta.get('signature_id', f'{match.namespace}.{match.rule}')))
        self.category = meta.get('category', meta.get('rule_group', 'info')).lower()
        self.malware_type = meta.get('malware_type', None)
        self.version = meta.get('version', meta.get('rule_version', meta.get('revision', 1)))
        self.description = meta.get('description', None)
        self.classification = meta.get('classification', Classification.UNRESTRICTED)
        self.source = meta.get('source', meta.get('organisation', None))
        self.summary = meta.get('summary', None)
        self.score_override = meta.get('al_score', None)
        self.author = meta.get('author', meta.get('poc', None))
        self.status = meta.get('status', None)  # Status assigned by the rule creator
        self.al_status = meta.get(self.status, meta.get('al_status', 'TESTING'))
        self.actor_type = meta.get('actor_type', meta.get('ta_type', None))
        self.mitre_att = meta.get('mitre_att', None)
        self.mitre_group = meta.get('mitre_group', None)

        def _safe_split(comma_sep_list):
            return [e for e in comma_sep_list.split(',') if e]

        # Specifics about the category
        self.info = meta.get('info', None)
        self.technique = meta.get('technique', None)
        self.exploit = meta.get('exploit', None)
        self.tool = meta.get('tool', None)
        self.malware = meta.get('malware', meta.get('implant', None))

        self.actors = _safe_split(meta.get('used_by', ''))
        self.summary = _safe_split(meta.get('summary', ''))
        self.exploits = _safe_split(meta.get('exploit', ''))

        # Parse and populate malware list
        self.malwares = []
        if self.malware:
            if ',' in self.malware:
                for malware in self.malware.split(','):
                    tokens = malware.split(':')
                    malware_name = tokens[0]
                    malware_family = tokens[1] if (len(tokens) == 2) else ''
                    self.malwares.append((malware_name.strip().upper(), malware_family.strip().upper()))
            else:
                self.malwares.append((self.malware, self.malware_type or ''))

        # Parse and populate technique info
        self.techniques = []
        if self.technique:
            if ',' in self.technique:
                for technique in self.technique.split(','):
                    tokens = technique.split(':')
                    category = ''
                    if len(tokens) == 2:
                        category = tokens[0]
                        name = tokens[1]
                    else:
                        name = tokens[0]
                    self.techniques.append((category.strip(), name.strip()))
            else:
                self.techniques.append((self.technique, self.technique))

        # Parse and populate info
        self.infos = []
        if self.info:
            if ',' in self.info:
                for info in self.info.split(','):
                    tokens = info.split(':', 1)
                    if len(tokens) == 2:
                        # category, value
                        self.infos.append((tokens[0], tokens[1]))
                    else:
                        self.infos.append((None, tokens[0]))
            else:
                self.infos.append((self.info, self.info))


class Yara(ServiceBase):
    TECHNIQUE_DESCRIPTORS = dict(
        shellcode=('technique.shellcode', 'Embedded shellcode'),
        packer=('technique.packer', 'Packed PE'),
        cryptography=('technique.crypto', 'Uses cryptography/compression'),
        obfuscation=('technique.obfuscation', 'Obfuscated'),
        keylogger=('technique.keylogger', 'Keylogging capability'),
        comms_routine=('technique.comms_routine', 'Does external comms'),
        persistance=('technique.persistence', 'Has persistence'),
    )

    YARA_HEURISTICS_MAP = dict(
        info=1,
        technique=2,
        exploit=3,
        tool=4,
        malware=5,
    )

    def __init__(self, config=None):
        super(Yara, self).__init__(config)
        self.rules = None
        self.rules_md5 = None
        self.initialization_lock = threading.RLock()
        self.task = None

        self.yara_externals = {f'al_{x}': x for x in ['submitter', 'mime', 'file_type']}

    def _add_resultinfo_for_match(self, result: Result, match):
        """
        Parse from Yara signature match and add information to the overall AL service result. This module determines
        result score and identifies any AL tags that should be added (i.e. IMPLANT_NAME, THREAT_ACTOR, etc.).

        Args:
            result: AL ResultSection object.
            match: Yara rules Match object item.

        Returns:
            None.
        """
        almeta = YaraMetadata(match)
        self._normalize_metadata(almeta)

        if not self.task.deep_scan and almeta.al_status == "NOISY":
            almeta.score_override = 0

        section = ResultSection('', classification=almeta.classification)
        section.set_heuristic(self.YARA_HEURISTICS_MAP.get(almeta.category, 1),
                              signature=f'{match.namespace}.{match.rule}', attack_id=almeta.mitre_att)
        section.add_tag('file.rule.yara', f'{match.namespace}.{match.rule}')

        title_elements = [f"[{match.namespace}] {match.rule}", ]

        if almeta.actor_type:
            section.add_tag('attribution.actor', almeta.actor_type)

        # Malware Tags
        implant_title_elements = []
        for (implant_name, implant_family) in almeta.malwares:
            if implant_name:
                implant_title_elements.append(implant_name)
                section.add_tag('attribution.implant', implant_name)
            if implant_family:
                implant_title_elements.append(implant_family)
                section.add_tag('attribution.family', implant_family)
        if implant_title_elements:
            title_elements.append(f"implant: {','.join(implant_title_elements)}")

        # Threat Actor metadata
        for actor in almeta.actors:
            title_elements.append(actor)
            section.add_tag('attribution.actor', actor)

        # Exploit / CVE metadata
        if almeta.exploits:
            title_elements.append(f" [Exploits(s): {','.join(almeta.exploits)}] ")
        for exploit in almeta.exploits:
            section.add_tag('attribution.exploit', exploit)

        # Include technique descriptions in the section summary
        summary_elements = set()
        for (category, name) in almeta.techniques:
            descriptor = self.TECHNIQUE_DESCRIPTORS.get(category, None)
            if descriptor:
                technique_type, technique_description = descriptor
                section.add_tag(technique_type, name)
                summary_elements.add(technique_description)

        for (category, value) in almeta.infos:
            if category == 'compiler':
                section.add_tag('file.compiler', value)
            elif category == 'libs':
                section.add_tag('file.lib', value)

        if summary_elements:
            title_elements.append(f" (Summary: {', '.join(summary_elements)})")
        for element in summary_elements:
            section.add_tag('file.behavior', element)

        title = " ".join(title_elements)
        section.title_text = title

        json_body = dict()

        json_body['name'] = f"[{match.namespace}] {match.rule}"

        if almeta.id and almeta.version and almeta.author:
            json_body.update(dict(
                id=almeta.id,
                version=almeta.version,
                author=almeta.author,
            ))

        if almeta.description:
            json_body['description'] = almeta.description

        string_match_data = self._add_string_match_data(match)
        if string_match_data:
            json_body['string_hits'] = string_match_data

        section.set_body(json.dumps(json_body), body_format=BODY_FORMAT.KEY_VALUE)

        result.add_section(section)
        # result.order_results_by_score() TODO: should v4 support this?

    def _add_string_match_data(self, match) -> List[str]:
        """
        Parses and adds matching strings from a Yara match object to an AL ResultSection.

        Args:
            match: Yara match object.

        Returns:
            None.
        """
        string_hits = []
        strings = match.strings
        string_dict = {}
        for offset, identifier, data in strings:
            if data not in string_dict:
                string_dict[data] = []
            string_dict[data].append((offset, identifier))

        result_dict = {}
        for string_value, string_list in string_dict.items():
            count = len(string_list)
            string_offset_list = []
            ident = ''
            for offset, ident in string_list[:5]:
                string_offset_list.append(str(hex(offset)).replace("L", ""))

            if ident == '$':
                string_name = ""
            else:
                string_name = f"{ident[1:]} "

            string_offset = ", ".join(string_offset_list)
            if len(string_list) > 5:
                string_offset += "..."

            is_wide_char = self._is_wide_char(string_value)
            if is_wide_char:
                string_value = self._get_non_wide_char(string_value)

            string_value = repr(string_value)
            if len(string_value) > 100:
                string_value = f"{string_value[:100]}..."

            wide_str = ""
            if is_wide_char:
                wide_str = " (wide)"

            entry_name = ''.join((string_name, wide_str))
            if string_name:
                result_list = result_dict.get(entry_name, [])
                result_list.append((string_value, string_offset, count))
                result_dict[entry_name] = result_list
                continue

            string_hit = f"{entry_name}: '{string_value} [@ {string_offset}]" \
                         f"{' (' + str(count) + 'x)' if count > 1 else ''}'"
            string_hits.append(string_hit)

        for entry_name, result_list in result_dict.items():
            for result in result_list[:5]:
                string_hit = f"{entry_name}: '{result[0]}' [@ {result[1]}]"\
                             f"{' (' + str(result[2]) + 'x)' if result[2] > 1 else ''}"
                string_hits.append(string_hit)
            more = len(result_list[5:])
            if more:
                string_hits.append(f"{entry_name} x{more}")

        return string_hits

    def _extract_result_from_matches(self, matches):
        """
        Iterate through Yara match object and send to parser.

        Args:
            matches: Yara rules Match object (list).

        Returns:
            AL Result object.
        """
        result = Result()
        for match in matches:
            self._add_resultinfo_for_match(result, match)
        return result

    @staticmethod
    def _get_non_wide_char(string: str) -> str:
        """
        Convert wide string to regular string.

        Args:
            string: Wide-character string to convert.

        Returns:
            Converted string.
        """
        res = []
        for (i, c) in enumerate(string):
            if i % 2 == 0:
                res.append(c)

        return ''.join(res)

    @staticmethod
    def _is_wide_char(string):
        """
        Determine if string is a wide-character string.

        Args:
            string: Potential wide-character string.

        Returns:
            True if wide character, or False.
        """
        if len(string) >= 2 and len(string) % 2 == 0:
            is_wide_char = True
            for (i, c) in enumerate(string):
                if ((i % 2 == 0 and c == 0) or
                        (i % 2 == 1 and c != 0)):
                    is_wide_char = False
                    break
        else:
            is_wide_char = False

        return is_wide_char

    @staticmethod
    def _normalize_metadata(almeta):
        """Convert classification to uppercase."""
        almeta.classification = almeta.classification.upper()

    def _load_rules(self):
        """
        Load Yara rules files. This function will check the updates directory and try to load the latest set of
        Yara rules files. If not successful, it will try older versions of the Yara rules files.
        """
        if not os.path.exists(FILE_UPDATE_DIRECTORY):
            raise Exception("Yara rules directory not found")

        yara_rules_dirs = [x for x in sorted(os.listdir(FILE_UPDATE_DIRECTORY), reverse=True) if not x.startswith('.tmp')]

        rules = None
        for yara_rules_dir in yara_rules_dirs:
            # Find all the .yar files
            yar_files = os.listdir(os.path.join(FILE_UPDATE_DIRECTORY, yara_rules_dir))
            if not yar_files:
                continue

            # Load all files in the dir, each file will have its own Yara namespace
            # Each file is expected to be from a different repo source
            # Using namespaces, allows us to handle Yara rule name conflicts
            filepaths = {os.path.splitext(os.path.basename(x))[0]: os.path.join(FILE_UPDATE_DIRECTORY, yara_rules_dir, x) for x in yar_files}

            self.log.info(f"YARA loaded rules from: {yara_rules_dir}")
            rules = yara.compile(filepaths=filepaths, externals=self.yara_externals)

            if rules:
                with self.initialization_lock:
                    self.rules = rules
                    # self.rules_md5 = rules_md5
                    return

        if not rules:
            raise Exception("No valid YARA rules files found")

    # noinspection PyBroadException
    def execute(self, request):
        """Main Module. See README for details."""
        if not self.rules:
            return

        self.task = request.task
        local_filename = request.file_path

        yara_externals = {}
        for k, i in self.yara_externals.items():
            # Check default request.task fields
            try:
                sval = self.task.get(i)
            except Exception:
                sval = None
            # if not sval:
            #     # Check metadata dictionary
            #     smeta = self.task.metadata
            #     if isinstance(smeta, dict):
            #         sval = smeta.get(i, None)
            if not sval:
                # Check params dictionary
                smeta = self.task.service_config
                if isinstance(smeta, dict):
                    sval = smeta.get(i, None)
            if not sval:
                # Check temp submission data
                smeta = self.task.temp_submission_data
                if isinstance(smeta, dict):
                    sval = smeta.get(i, None)
            # Create dummy value if item not found
            if not sval:
                sval = i

            # Normalize unicode with safe_str and make sure everything else is a string
            yara_externals[k] = str(safe_str(sval))

        with self.initialization_lock:
            try:
                matches = self.rules.match(local_filename, externals=yara_externals)
                request.result = self._extract_result_from_matches(matches)
            except Exception as e:
                # Internal error 30 == exceeded max string matches on rule
                if e != "internal error: 30":
                    raise
                else:
                    try:
                        # Fast mode == Yara skips strings already found
                        matches = self.rules.match(local_filename, externals=yara_externals, fast=True)
                        result = self._extract_result_from_matches(matches)
                        section = ResultSection("Service Warnings")
                        section.add_line("Too many matches detected with current ruleset. "
                                         "YARA forced to scan in fast mode.")
                        request.result = result
                    except:
                        self.log.warning(f"YARA internal error 30 detected on submission {self.task.sid}")
                        result = Result()
                        section = ResultSection("YARA scan not completed.")
                        section.add_line("File returned too many matches with current rule set and YARA exited.")
                        result.add_section(section)
                        request.result = result

    def get_service_version(self):
        basic_version = super(Yara, self).get_service_version()
        return f'{basic_version}.r{self.rules_md5 or "0"}'

    def start(self):
        # Set configuration flags to 4 times the default
        yara.set_config(max_strings_per_rule=40000, stack_size=65536)

        try:
            # Load the rules
            self._load_rules()

        except Exception as e:
            raise Exception(f"Something went wrong while trying to load YARA rules: {str(e)}")

        self.log.info(f"YARA started with service version: {self.get_service_version()}")