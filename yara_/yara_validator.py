import json
import logging
import os
import re
import subprocess
import tempfile

from assemblyline.common.str_utils import safe_str


class YaraValidator(object):

    def __init__(self, externals=None, logger=None):
        if not logger:
            from assemblyline.common import log as al_log
            al_log.init_logging('YaraValidator')
            logger = logging.getLogger('assemblyline.yara_validator')
            logger.setLevel(logging.WARNING)
        if not externals:
            externals = {'dummy': ''}
        self.log = logger
        self.externals = externals
        self.rulestart = re.compile(r'^(?:global )?(?:private )?(?:private )?rule ', re.MULTILINE)
        self.rulename = re.compile('rule ([^{^:]+)')

    def clean(self, rulefile, eline, message, invalid_rule_name):
        with open(rulefile, 'r') as f:
            f_lines = f.readlines()
        # List will start at 0 not 1
        error_line = eline - 1

        if invalid_rule_name:
            f_lines[error_line] = f_lines[error_line].replace(invalid_rule_name, f"{invalid_rule_name}_1")
            self.log.warning(f"Yara rule '{invalid_rule_name}' was renamed '{invalid_rule_name}_1' because it's "
                             f"rule name was used more then once.")
        else:
            # First loop to find start of rule
            start_idx = 0
            while True:
                find_start = error_line - start_idx
                if find_start == -1:
                    raise Exception("Yara Validator failed to find invalid rule start. "
                                    f"Yara Error: {message} Line: {eline}")
                line = f_lines[find_start]
                if re.match(self.rulestart, line):
                    invalid_rule_name = re.search(self.rulename, line).group(1).strip()

                    # Second loop to find end of rule
                    end_idx = 0
                    while True:
                        find_end = error_line + end_idx
                        if find_end >= len(f_lines):
                            raise Exception("Yara Validator failed to find invalid rule end. "
                                            f"Yara Error: {message} Line: {eline}")
                        line = f_lines[find_end]
                        if re.match(self.rulestart, line) or find_end == len(f_lines) - 1:
                            # Now we have the start and end, strip from file
                            if find_end == len(f_lines) - 1:
                                f_lines = f_lines[:find_start]
                            else:
                                f_lines = f_lines[:find_start] + f_lines[find_end:]
                            break
                        end_idx += 1
                    # Send the error output to AL logs
                    error_message = f"Yara rule '{invalid_rule_name}' removed from rules file because of an error " \
                                    f"at line {eline} [{message}]."
                    self.log.warning(error_message)
                    break
                start_idx += 1

        with open(rulefile, 'w') as f:
            f.writelines(f_lines)

        return invalid_rule_name

    def paranoid_rule_check(self, rulefile):
        # Run rules separately on command line to ensure there are no errors
        print_val = "--==Rules_validated++__"
        external_file = os.path.join(tempfile.gettempdir(), "externals.json")
        try:
            with open(external_file, "wb") as out_json:
                out_json.write(json.dumps(self.externals).encode("utf-8"))

            p = subprocess.Popen(f"python3 paranoid_check.py {rulefile} {external_file}",
                                 cwd=os.path.dirname(os.path.realpath(__file__)),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
            stdout, stderr = p.communicate()

        finally:
            os.unlink(external_file)

        stdout = safe_str(stdout)
        stderr = safe_str(stderr)

        if print_val not in stdout:
            if stdout.strip().startswith('yara.SyntaxError'):
                raise Exception(stdout.strip())
            else:
                raise Exception("YaraValidator has failed!--+--" + str(stderr) + "--:--" + str(stdout))

    def validate_rules(self, rulefile):
        change = False
        while True:
            try:
                self.paranoid_rule_check(rulefile)
                return change

            # If something goes wrong, clean rules until valid file given
            except Exception as e:
                error = str(e)
                change = True
                if error.startswith('yara.SyntaxError'):

                    e_line = int(error.split('):', 1)[0].split("(", -1)[1])
                    e_message = error.split("): ", 1)[1]
                    if "duplicated identifier" in error:
                        invalid_rule_name = e_message.split('"')[1]
                    else:
                        invalid_rule_name = ""
                    try:
                        self.clean(rulefile, e_line, e_message, invalid_rule_name)
                    except Exception as ve:
                        raise ve

                else:
                    raise e

                continue
