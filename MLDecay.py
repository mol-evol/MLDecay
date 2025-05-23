#!/usr/bin/env python3

import os
import sys
import argparse
import numpy as np
from Bio import Phylo, AlignIO, SeqIO
import tempfile
import shutil
import subprocess
import logging
import re
import multiprocessing
import time
import datetime
from pathlib import Path

VERSION = "1.0.3"
# Set up logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

# --- Constants for Filenames ---
NEXUS_ALIGNMENT_FN = "alignment.nex"
ML_TREE_FN = "ml_tree.tre"
ML_SCORE_FN = "ml_score.txt"
ML_SEARCH_NEX_FN = "ml_search.nex"
ML_LOG_FN = "paup_ml.log"

AU_TEST_NEX_FN = "au_test.nex"
AU_TEST_SCORE_FN = "au_test_results.txt"
AU_LOG_FN = "paup_au.log"


class MLDecayIndices:
    """
    Implements ML-based phylogenetic decay indices (Bremer support) using PAUP*.
    Calculates support by comparing optimal tree likelihood with constrained trees,
    using PAUP*'s backbone constraint followed by reverse constraints and AU test.
    """

    def __init__(self, alignment_file, alignment_format="fasta", model="GTR+G",
                 temp_dir: Path = None, paup_path="paup", threads="auto",
                 starting_tree: Path = None, data_type="dna",
                 debug=False, keep_files=False, gamma_shape=None, prop_invar=None,
                 base_freq=None, rates=None, protein_model=None, nst=None,
                 parsmodel=None, paup_block=None):

        self.alignment_file = Path(alignment_file)
        self.alignment_format = alignment_format
        self.model_str = model # Keep original model string for reference
        self.paup_path = paup_path
        self.starting_tree = starting_tree # Already a Path or None from main
        self.debug = debug
        self.keep_files = keep_files or debug
        self.gamma_shape_arg = gamma_shape
        self.prop_invar_arg = prop_invar
        self.base_freq_arg = base_freq
        self.rates_arg = rates
        self.protein_model_arg = protein_model
        self.nst_arg = nst
        self.parsmodel_arg = parsmodel # For discrete data, used in _convert_model_to_paup
        self.user_paup_block = paup_block # Raw user block content
        self._files_to_cleanup = []

        self.data_type = data_type.lower()
        if self.data_type not in ["dna", "protein", "discrete"]:
            logger.warning(f"Unknown data type: {data_type}, defaulting to DNA")
            self.data_type = "dna"

        if threads == "auto":
            total_cores = multiprocessing.cpu_count()
            if total_cores > 2:
                self.threads = total_cores - 2 # Leave 2 cores for OS/other apps
            elif total_cores > 1:
                self.threads = total_cores - 1 # Leave 1 core
            else:
                self.threads = 1 # Use 1 core if only 1 is available
            logger.info(f"Using 'auto' threads: PAUP* will be configured for {self.threads} thread(s) (leaving some for system).")
        elif str(threads).lower() == "all": # Add an explicit "all" option if you really want it
            self.threads = multiprocessing.cpu_count()
            logger.warning(f"PAUP* configured to use ALL {self.threads} threads. System may become unresponsive.")
        else:
            try:
                self.threads = int(threads)
                if self.threads < 1:
                    logger.warning(f"Thread count {self.threads} is invalid, defaulting to 1.")
                    self.threads = 1
                elif self.threads > multiprocessing.cpu_count():
                    logger.warning(f"Requested {self.threads} threads, but only {multiprocessing.cpu_count()} cores available. Using {multiprocessing.cpu_count()}.")
                    self.threads = multiprocessing.cpu_count()
            except ValueError:
                logger.warning(f"Invalid thread count '{threads}', defaulting to 1.")
                self.threads = 1

        logger.info(f"PAUP* will be configured to use up to {self.threads} thread(s).")

        # --- Temporary Directory Setup ---
        self._temp_dir_obj = None  # For TemporaryDirectory lifecycle
        if self.debug or self.keep_files or temp_dir:
            if temp_dir: # User-provided temp_dir (already a Path object)
                self.temp_path = temp_dir
                self.temp_path.mkdir(parents=True, exist_ok=True)
            else: # Debug/keep_files, create a timestamped dir
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                debug_runs_path = Path.cwd() / "debug_runs"
                debug_runs_path.mkdir(parents=True, exist_ok=True)
                self.work_dir_name = f"mldecay_{timestamp}"
                self.temp_path = debug_runs_path / self.work_dir_name
                self.temp_path.mkdir(parents=True, exist_ok=True)
            logger.info(f"Using temporary directory: {self.temp_path}")
        else: # Auto-cleanup
            self._temp_dir_obj = tempfile.TemporaryDirectory(prefix="mldecay_")
            self.temp_path = Path(self._temp_dir_obj.name)
            self.work_dir_name = self.temp_path.name
            logger.info(f"Using temporary directory (auto-cleanup): {self.temp_path}")

        # --- PAUP* Model Settings ---
        self.parsmodel = False # Default, will be set by _convert_model_to_paup if discrete
        if self.user_paup_block is None:
            self.paup_model_cmds = self._convert_model_to_paup(
                self.model_str, gamma_shape=self.gamma_shape_arg, prop_invar=self.prop_invar_arg,
                base_freq=self.base_freq_arg, rates=self.rates_arg,
                protein_model=self.protein_model_arg, nst=self.nst_arg,
                parsmodel_user_intent=self.parsmodel_arg # Pass user intent
            )
        else:
            logger.info("Using user-provided PAUP block for model specification.")
            self.paup_model_cmds = self.user_paup_block # This is the content of the block

        # --- Alignment Handling ---
        try:
            self.alignment = AlignIO.read(str(self.alignment_file), self.alignment_format)
            logger.info(f"Loaded alignment: {len(self.alignment)} sequences, {self.alignment.get_alignment_length()} sites.")
        except Exception as e:
            logger.error(f"Failed to load alignment '{self.alignment_file}': {e}")
            if self._temp_dir_obj: self._temp_dir_obj.cleanup() # Manual cleanup if init fails early
            raise

        if self.data_type == "discrete":
            if not self._validate_discrete_data():
                logger.warning("Discrete data validation failed based on content, proceeding but results may be unreliable.")

        if self.keep_files or self.debug: # Copy original alignment for debugging
             shutil.copy(str(self.alignment_file), self.temp_path / f"original_alignment.{self.alignment_format}")

        self.nexus_file_path = self.temp_path / NEXUS_ALIGNMENT_FN
        self._convert_to_nexus() # Writes to self.nexus_file_path

        self.ml_tree = None
        self.ml_likelihood = None
        self.decay_indices = {}

    def __del__(self):
        """Cleans up temporary files if TemporaryDirectory object was used."""
        if hasattr(self, '_temp_dir_obj') and self._temp_dir_obj:
            logger.debug(f"Attempting to cleanup temp_dir_obj for {self.temp_path}")
            self._temp_dir_obj.cleanup()
            logger.info(f"Auto-cleaned temporary directory: {self.temp_path}")
        elif hasattr(self, 'temp_path') and self.temp_path.exists() and not self.keep_files:
            logger.info(f"Manually cleaning up temporary directory: {self.temp_path}")
            shutil.rmtree(self.temp_path)
        elif hasattr(self, 'temp_path') and self.keep_files:
            logger.info(f"Keeping temporary directory: {self.temp_path}")

    def _convert_model_to_paup(self, model_str, gamma_shape, prop_invar, base_freq, rates, protein_model, nst, parsmodel_user_intent):
        """Converts model string and params to PAUP* 'lset' command part (without 'lset' itself)."""
        cmd_parts = []
        has_gamma = "+G" in model_str.upper()
        has_invar = "+I" in model_str.upper()
        base_model_name = model_str.split("+")[0].upper()

        if self.data_type == "dna":
            if nst is not None: cmd_parts.append(f"nst={nst}")
            elif base_model_name == "GTR": cmd_parts.append("nst=6")
            elif base_model_name in ["HKY", "K2P", "K80", "TN93"]: cmd_parts.append("nst=2")
            elif base_model_name in ["JC", "JC69", "F81"]: cmd_parts.append("nst=1")
            else:
                logger.warning(f"Unknown DNA model: {base_model_name}, defaulting to GTR (nst=6).")
                cmd_parts.append("nst=6")

            current_nst = next((p.split('=')[1] for p in cmd_parts if "nst=" in p), None)
            if current_nst == '6' or (base_model_name == "GTR" and nst is None):
                cmd_parts.append("rmatrix=estimate")
            elif current_nst == '2' or (base_model_name in ["HKY", "K2P"] and nst is None):
                cmd_parts.append("tratio=estimate")

            if base_freq: cmd_parts.append(f"basefreq={base_freq}")
            elif base_model_name in ["JC", "K2P", "JC69", "K80"] : cmd_parts.append("basefreq=equal")
            else: cmd_parts.append("basefreq=estimate") # GTR, HKY, F81, TN93 default to estimate

        elif self.data_type == "protein":
            valid_protein_models = ["JTT", "WAG", "LG", "DAYHOFF", "MTREV", "CPREV", "BLOSUM62", "HIVB", "HIVW"]
            if protein_model: cmd_parts.append(f"protein={protein_model.lower()}")
            elif base_model_name.upper() in valid_protein_models: cmd_parts.append(f"protein={base_model_name.lower()}")
            else:
                logger.warning(f"Unknown protein model: {base_model_name}, defaulting to JTT.")
                cmd_parts.append("protein=jtt")

        elif self.data_type == "discrete": # Typically Mk model
            cmd_parts.append("nst=1") # For standard Mk
            if base_freq: cmd_parts.append(f"basefreq={base_freq}")
            else: cmd_parts.append("basefreq=equal") # Default for Mk

            if parsmodel_user_intent is None: # If user didn't specify, default to True for discrete
                self.parsmodel = True
            else:
                self.parsmodel = bool(parsmodel_user_intent)


        # Common rate variation and invariable sites for all data types
        if rates: cmd_parts.append(f"rates={rates}")
        elif has_gamma: cmd_parts.append("rates=gamma")
        else: cmd_parts.append("rates=equal")

        current_rates = next((p.split('=')[1] for p in cmd_parts if "rates=" in p), "equal")
        if gamma_shape is not None and (current_rates == "gamma" or has_gamma):
            cmd_parts.append(f"shape={gamma_shape}")
        elif current_rates == "gamma" or has_gamma:
            cmd_parts.append("shape=estimate")

        if prop_invar is not None:
            cmd_parts.append(f"pinvar={prop_invar}")
        elif has_invar:
            cmd_parts.append("pinvar=estimate")
        else: # No +I and no explicit prop_invar
            cmd_parts.append("pinvar=0")

        return "lset " + " ".join(cmd_parts) + ";"

    def _validate_discrete_data(self):
        """Validate that discrete data contains only 0, 1, -, ? characters."""
        if self.data_type == "discrete":
            valid_chars = set("01-?")
            for record in self.alignment:
                seq_chars = set(str(record.seq).upper()) # Convert to upper for case-insensitivity if needed
                invalid_chars = seq_chars - valid_chars
                if invalid_chars:
                    logger.warning(f"Sequence {record.id} contains invalid discrete characters: {invalid_chars}. Expected only 0, 1, -, ?.")
                    return False
        return True

    def _format_taxon_for_paup(self, taxon_name):
        """Format a taxon name for PAUP* (handles spaces, special chars by quoting)."""
        if not isinstance(taxon_name, str): taxon_name = str(taxon_name)
        # PAUP* needs quotes if name contains whitespace or NEXUS special chars: ( ) [ ] { } / \ , ; = * ` " ' < >
        if re.search(r'[\s\(\)\[\]\{\}/\\,;=\*`"\'<>]', taxon_name) or ':' in taxon_name: # Colon also problematic
            return f"'{taxon_name.replace(chr(39), '_')}'" # chr(39) is single quote

        return taxon_name

    def _convert_to_nexus(self):
        """Converts alignment to NEXUS, writes to self.nexus_file_path."""
        try:
            with open(self.nexus_file_path, 'w') as f:
                f.write("#NEXUS\n\n")
                f.write("BEGIN DATA;\n")
                dt = "DNA"
                if self.data_type == "protein": dt = "PROTEIN"
                elif self.data_type == "discrete": dt = "STANDARD"

                f.write(f"  DIMENSIONS NTAX={len(self.alignment)} NCHAR={self.alignment.get_alignment_length()};\n")
                format_line = f"  FORMAT DATATYPE={dt} MISSING=? GAP=- INTERLEAVE=NO"
                if self.data_type == "discrete":
                    format_line += " SYMBOLS=\"01\"" # Assuming binary discrete data
                f.write(format_line + ";\n")
                f.write("  MATRIX\n")
                for record in self.alignment:
                    f.write(f"  {self._format_taxon_for_paup(record.id)} {record.seq}\n")
                f.write("  ;\nEND;\n")

                if self.data_type == "discrete":
                    f.write("\nBEGIN ASSUMPTIONS;\n")
                    f.write("  OPTIONS DEFTYPE=UNORD POLYTCOUNT=MINSTEPS;\n") # Common for Mk
                    f.write("END;\n")
            logger.info(f"Converted alignment to NEXUS: {self.nexus_file_path}")
        except Exception as e:
            logger.error(f"Failed to convert alignment to NEXUS: {e}")
            raise

    def _get_paup_model_setup_cmds(self):
        """Returns the model setup command string(s) for PAUP* script."""
        if self.user_paup_block is None:
            # self.paup_model_cmds is like "lset nst=6 ...;"
            # Remove "lset " for combining with nthreads, keep ";"
            model_params_only = self.paup_model_cmds.replace("lset ", "", 1)
            base_cmds = [
                f"lset nthreads={self.threads} {model_params_only}", # model_params_only includes the trailing ";"
                "set criterion=likelihood;"
            ]
            if self.data_type == "discrete":
                base_cmds.append("options deftype=unord polytcount=minsteps;")
                if self.parsmodel: # self.parsmodel is set by _convert_model_to_paup
                    base_cmds.append("set parsmodel=yes;")
            return "\n".join(f"    {cmd}" for cmd in base_cmds)
        else:
            # self.paup_model_cmds is the user's raw block content
            # Assume it sets threads, model, criterion, etc.
            return self.paup_model_cmds # Return as is, for direct insertion

    def _run_paup_command_file(self, paup_cmd_filename_str: str, log_filename_str: str, timeout_sec: int = None):
        """Runs a PAUP* .nex command file located in self.temp_path."""
        paup_cmd_file = self.temp_path / paup_cmd_filename_str
        # The main log file will capture both stdout and stderr from PAUP*
        combined_log_file_path = self.temp_path / log_filename_str

        if not paup_cmd_file.exists():
            logger.error(f"PAUP* command file not found: {paup_cmd_file}")
            raise FileNotFoundError(f"PAUP* command file not found: {paup_cmd_file}")

        logger.info(f"Running PAUP* command file: {paup_cmd_filename_str} (Log: {log_filename_str})")

        # stdout_content and stderr_content will be filled for logging/debugging if needed
        stdout_capture = ""
        stderr_capture = ""

        try:
            # Open the log file once for both stdout and stderr
            with open(combined_log_file_path, 'w') as f_log:
                process = subprocess.Popen(
                    [self.paup_path, "-n", paup_cmd_filename_str],
                    cwd=str(self.temp_path),
                    stdout=subprocess.PIPE, # Capture stdout
                    stderr=subprocess.PIPE, # Capture stderr
                    text=True,
                    universal_newlines=True # For text=True
                )

                # Read stdout and stderr in a non-blocking way or use communicate
                # communicate() is simpler and safer for handling potential deadlocks
                try:
                    stdout_capture, stderr_capture = process.communicate(timeout=timeout_sec)
                except subprocess.TimeoutExpired:
                    process.kill() # Ensure process is killed on timeout
                    stdout_capture, stderr_capture = process.communicate() # Try to get any remaining output
                    logger.error(f"PAUP* command {paup_cmd_filename_str} timed out after {timeout_sec}s.")
                    f_log.write(f"--- PAUP* Execution Timed Out ({timeout_sec}s) ---\n")
                    if stdout_capture: f_log.write("--- STDOUT (partial) ---\n" + stdout_capture)
                    if stderr_capture: f_log.write("\n--- STDERR (partial) ---\n" + stderr_capture)
                    raise # Re-raise the TimeoutExpired exception

                # Write captured output to the log file
                f_log.write("--- STDOUT ---\n")
                f_log.write(stdout_capture if stdout_capture else "No stdout captured.\n")
                if stderr_capture:
                    f_log.write("\n--- STDERR ---\n")
                    f_log.write(stderr_capture)

                retcode = process.returncode
                if retcode != 0:
                    logger.error(f"PAUP* execution failed for {paup_cmd_filename_str}. Exit code: {retcode}")
                    # The log file already contains stdout/stderr
                    logger.error(f"PAUP* stdout/stderr saved to {combined_log_file_path}. Stderr sample: {stderr_capture[:500]}...")
                    # Raise an equivalent of CalledProcessError
                    raise subprocess.CalledProcessError(retcode, process.args, output=stdout_capture, stderr=stderr_capture)

            if self.debug:
                logger.debug(f"PAUP* output saved to: {combined_log_file_path}")
                logger.debug(f"PAUP* stdout sample (from capture):\n{stdout_capture[:500]}...")
                if stderr_capture: logger.debug(f"PAUP* stderr sample (from capture):\n{stderr_capture[:500]}...")

            # Return a simple object that mimics CompletedProcess for the parts we use
            # Or adjust callers to expect (stdout_str, stderr_str, retcode) tuple
            class MockCompletedProcess:
                def __init__(self, args, returncode, stdout, stderr):
                    self.args = args
                    self.returncode = returncode
                    self.stdout = stdout
                    self.stderr = stderr

            return MockCompletedProcess(process.args, retcode, stdout_capture, stderr_capture)

        except subprocess.CalledProcessError: # Already logged, just re-raise
            raise
        except subprocess.TimeoutExpired: # Already logged, just re-raise
            raise
        except Exception as e:
            # Fallback for other errors during Popen or communicate
            logger.error(f"Unexpected error running PAUP* for {paup_cmd_filename_str}: {e}")
            # Attempt to write to log if f_log was opened
            if 'f_log' in locals() and not f_log.closed:
                 f_log.write(f"\n--- Script Error during PAUP* execution ---\n{str(e)}\n")
            raise

    def _parse_likelihood_from_score_file(self, score_file_path: Path):
        if not score_file_path.exists():
            logger.warning(f"Score file not found: {score_file_path}")
            return None
        try:
            content = score_file_path.read_text()
            if self.debug: logger.debug(f"Score file ({score_file_path}) content:\n{content}")

            lines = content.splitlines()
            header_idx, lnl_col_idx = -1, -1

            for i, line_text in enumerate(lines):
                norm_line = ' '.join(line_text.strip().lower().split()) # Normalize
                if "tree" in norm_line and ("-lnl" in norm_line or "loglk" in norm_line or "likelihood" in norm_line):
                    header_idx = i
                    headers = norm_line.split()
                    for col_name in ["-lnl", "loglk", "likelihood", "-loglk"]:
                        if col_name in headers:
                            lnl_col_idx = headers.index(col_name)
                            break
                    if lnl_col_idx != -1: break

            if header_idx == -1 or lnl_col_idx == -1:
                logger.warning(f"Could not find valid header or likelihood column in {score_file_path}.")
                return None
            logger.debug(f"Found LNL column at index {lnl_col_idx} in header: {lines[header_idx].strip()}")

            for i in range(header_idx + 1, len(lines)):
                data_line_text = lines[i].strip()
                if not data_line_text: continue # Skip empty

                parts = data_line_text.split()
                if len(parts) > lnl_col_idx:
                    try:
                        val_str = parts[lnl_col_idx]
                        if '*' in val_str : # Handle cases like '**********' or if PAUP adds flags
                            logger.warning(f"Likelihood value problematic (e.g., '******') in {score_file_path}, line: '{data_line_text}'")
                            continue # Try next line if multiple scores
                        likelihood = float(val_str)
                        logger.info(f"Parsed log-likelihood from {score_file_path}: {likelihood}")
                        return likelihood
                    except ValueError:
                        logger.warning(f"Could not convert LNL value to float: '{parts[lnl_col_idx]}' from line '{data_line_text}' in {score_file_path}")
                else: logger.warning(f"Insufficient columns in data line: '{data_line_text}' in {score_file_path}")
            logger.warning(f"No parsable data lines found after header in {score_file_path}")
            return None
        except Exception as e:
            logger.warning(f"Error reading/parsing score file {score_file_path}: {e}")
            return None

    def build_ml_tree(self):
        logger.info("Building maximum likelihood tree...")
        script_cmds = [f"execute {NEXUS_ALIGNMENT_FN};", self._get_paup_model_setup_cmds()]

        if self.user_paup_block is None: # Standard model processing, add search commands
            if self.starting_tree and self.starting_tree.exists():
                start_tree_fn_temp = "start_tree.tre" # Relative to temp_path
                shutil.copy(str(self.starting_tree), str(self.temp_path / start_tree_fn_temp))
                script_cmds.extend([
                    f"gettrees file={start_tree_fn_temp};",
                    "lscores 1 / userbrlen=yes;", "hsearch start=current;"
                ])
            elif self.starting_tree: # Path provided but not found
                 logger.warning(f"Starting tree file not found: {self.starting_tree}. Performing standard search.")
                 script_cmds.append("hsearch start=stepwise addseq=random nreps=10;")
            else: # No starting tree
                script_cmds.append("hsearch start=stepwise addseq=random nreps=10;")

            script_cmds.extend([
                f"savetrees file={ML_TREE_FN} format=newick brlens=yes replace=yes;",
                f"lscores 1 / scorefile={ML_SCORE_FN} replace=yes;"
            ])
        else: # User-provided PAUP block, assume it handles search & save. Add defensively if not detected.
            block_lower = self.user_paup_block.lower()
            if "savetrees" not in block_lower:
                script_cmds.append(f"savetrees file={ML_TREE_FN} format=newick brlens=yes replace=yes;")
            if "lscores" not in block_lower and "lscore" not in block_lower : # Check for lscore too
                script_cmds.append(f"lscores 1 / scorefile={ML_SCORE_FN} replace=yes;")

        paup_script_content = f"#NEXUS\nbegin paup;\n" + "\n".join(script_cmds) + "\nquit;\nend;\n"
        ml_search_cmd_path = self.temp_path / ML_SEARCH_NEX_FN
        ml_search_cmd_path.write_text(paup_script_content)
        if self.debug: logger.debug(f"ML search PAUP* script ({ml_search_cmd_path}):\n{paup_script_content}")

        try:
            paup_result = self._run_paup_command_file(ML_SEARCH_NEX_FN, ML_LOG_FN, timeout_sec=3600) # 1hr timeout

            self.ml_likelihood = self._parse_likelihood_from_score_file(self.temp_path / ML_SCORE_FN)
            if self.ml_likelihood is None and paup_result.stdout: # Fallback to log
                logger.info(f"Fallback: Parsing ML likelihood from PAUP* log {ML_LOG_FN}")
                patterns = [r'-ln\s*L\s*=\s*([0-9.]+)', r'likelihood\s*=\s*([0-9.]+)', r'score\s*=\s*([0-9.]+)']
                for p in patterns:
                    m = re.findall(p, paup_result.stdout, re.IGNORECASE)
                    if m: self.ml_likelihood = float(m[-1]); break
                if self.ml_likelihood: logger.info(f"Extracted ML likelihood from log: {self.ml_likelihood}")
                else: logger.warning("Could not extract ML likelihood from PAUP* log.")

            ml_tree_path = self.temp_path / ML_TREE_FN
            if ml_tree_path.exists() and ml_tree_path.stat().st_size > 0:
                # Clean the tree file if it has metadata after semicolon
                cleaned_tree_path = self._clean_newick_tree(ml_tree_path)
                self.ml_tree = Phylo.read(str(cleaned_tree_path), "newick")
                logger.info(f"Successfully built ML tree. Log-likelihood: {self.ml_likelihood if self.ml_likelihood is not None else 'N/A'}")
                if self.ml_likelihood is None:
                    logger.error("ML tree built, but likelihood could not be determined. Analysis may be compromised.")
                    # Decide if this is a fatal error for downstream steps
            else:
                logger.error(f"ML tree file {ml_tree_path} not found or is empty after PAUP* run.")
                raise FileNotFoundError(f"ML tree file missing or empty: {ml_tree_path}")
        except Exception as e:
            logger.error(f"ML tree construction failed: {e}")
            raise # Re-raise to be handled by the main try-except block

    def _clean_newick_tree(self, tree_path, delete_cleaned=True):
        """
        Clean Newick tree files that may have metadata after the semicolon.

        Args:
            tree_path: Path to the tree file
            delete_cleaned: Whether to delete the cleaned file after use (if caller manages reading)

        Returns:
            Path to a cleaned tree file or the original path if no cleaning was needed
        """
        try:
            content = Path(tree_path).read_text()

            # Check if there's any text after a semicolon (including whitespace)
            semicolon_match = re.search(r';(.+)', content, re.DOTALL)
            if semicolon_match:
                # Get everything up to the first semicolon
                clean_content = content.split(';')[0] + ';'

                # Write the cleaned tree to a new file
                cleaned_path = Path(str(tree_path) + '.cleaned')
                cleaned_path.write_text(clean_content)

                # Mark the file for later deletion if requested
                if delete_cleaned:
                    self._files_to_cleanup.append(cleaned_path)

                if self.debug:
                    logger.debug(f"Original tree content: '{content}'")
                    logger.debug(f"Cleaned tree content: '{clean_content}'")

                logger.info(f"Cleaned tree file {tree_path} - removed metadata after semicolon")
                return cleaned_path

            return tree_path  # No cleaning needed
        except Exception as e:
            logger.warning(f"Error cleaning Newick tree {tree_path}: {e}")
            if self.debug:
                import traceback
                logger.debug(f"Traceback for tree cleaning error: {traceback.format_exc()}")
            return tree_path  # Return original path if cleaning fails

    def run_bootstrap_analysis(self, num_replicates=100):
        """
        Run bootstrap analysis with PAUP* to calculate support values.

        Args:
            num_replicates: Number of bootstrap replicates to perform

        Returns:
            The bootstrap consensus tree with support values, or None if analysis failed
        """
        # Define bootstrap constants
        BOOTSTRAP_NEX_FN = "bootstrap_search.nex"
        BOOTSTRAP_LOG_FN = "paup_bootstrap.log"
        BOOTSTRAP_TREE_FN = "bootstrap_trees.tre"

        logger.info(f"Running bootstrap analysis with {num_replicates} replicates...")

        script_cmds = [f"execute {NEXUS_ALIGNMENT_FN};", self._get_paup_model_setup_cmds()]

        # Add bootstrap commands
        script_cmds.extend([
            f"bootstrap nreps={num_replicates} search=heuristic keepall=no treefile={BOOTSTRAP_TREE_FN} replace=yes / start=stepwise addseq=random nreps=1;",
            f"savetrees file={BOOTSTRAP_TREE_FN} format=newick brlens=yes replace=yes supportValues=nodeLabels;"
        ])

        # Create and execute PAUP script
        paup_script_content = f"#NEXUS\nbegin paup;\n" + "\n".join(script_cmds) + "\nquit;\nend;\n"
        bootstrap_cmd_path = self.temp_path / BOOTSTRAP_NEX_FN
        bootstrap_cmd_path.write_text(paup_script_content)

        if self.debug: logger.debug(f"Bootstrap PAUP* script ({bootstrap_cmd_path}):\n{paup_script_content}")

        try:
            # Run the bootstrap analysis - timeout based on number of replicates
            self._run_paup_command_file(BOOTSTRAP_NEX_FN, BOOTSTRAP_LOG_FN,
                                      timeout_sec=max(3600, 60 * num_replicates))

            # Get the bootstrap tree
            bootstrap_tree_path = self.temp_path / BOOTSTRAP_TREE_FN

            if bootstrap_tree_path.exists() and bootstrap_tree_path.stat().st_size > 0:
                # Log the bootstrap tree file content for debugging
                if self.debug:
                    bootstrap_content = bootstrap_tree_path.read_text()
                    logger.debug(f"Bootstrap tree file content:\n{bootstrap_content}")

                # Clean the tree file if it has metadata after semicolon
                cleaned_tree_path = self._clean_newick_tree(bootstrap_tree_path)

                # Log the cleaned bootstrap tree file for debugging
                if self.debug:
                    cleaned_content = cleaned_tree_path.read_text() if Path(cleaned_tree_path).exists() else "Cleaning failed"
                    logger.debug(f"Cleaned bootstrap tree file content:\n{cleaned_content}")

                try:
                    # Parse bootstrap values from tree file
                    bootstrap_tree = Phylo.read(str(cleaned_tree_path), "newick")
                    self.bootstrap_tree = bootstrap_tree

                    # Verify that bootstrap values are present
                    has_bootstrap_values = False
                    for node in bootstrap_tree.get_nonterminals():
                        if node.confidence is not None:
                            has_bootstrap_values = True
                            break

                    if has_bootstrap_values:
                        logger.info(f"Bootstrap analysis complete with {num_replicates} replicates and bootstrap values")
                    else:
                        logger.warning(f"Bootstrap tree found, but no bootstrap values detected. Check PAUP* output format.")

                    return bootstrap_tree
                except Exception as parse_error:
                    logger.error(f"Error parsing bootstrap tree: {parse_error}")
                    if self.debug:
                        import traceback
                        logger.debug(f"Traceback for bootstrap parse error: {traceback.format_exc()}")
                    return None
            else:
                logger.error(f"Bootstrap tree file not found or empty: {bootstrap_tree_path}")
                return None
        except Exception as e:
            logger.error(f"Bootstrap analysis failed: {e}")
            if self.debug:
                import traceback
                logger.debug(f"Traceback: {traceback.format_exc()}")
            return None

    def run_au_test(self, tree_filenames_relative: list):
        if not tree_filenames_relative:
            logger.error("No tree files for AU test.")
            return None
        num_trees = len(tree_filenames_relative)
        if num_trees < 2: # AU test is meaningful for comparing multiple trees
            logger.warning(f"AU test needs >= 2 trees; {num_trees} provided. Skipping AU test.")
            # If it's just the ML tree, we can return its own info conventionally
            if num_trees == 1 and tree_filenames_relative[0] == ML_TREE_FN and self.ml_likelihood is not None:
                return {1: {'lnL': self.ml_likelihood, 'AU_pvalue': 1.0}} # Best tree p-val = 1
            return None

        script_cmds = [f"execute {NEXUS_ALIGNMENT_FN};", self._get_paup_model_setup_cmds()]
        script_cmds.append(f"gettrees file={tree_filenames_relative[0]} mode=3 storebrlens=yes;")
        for rel_fn in tree_filenames_relative[1:]:
            script_cmds.append(f"gettrees file={rel_fn} mode=7 storebrlens=yes;")

        # AU_TEST_SCORE_FN is the file PAUP* writes scores to. We parse from AU_LOG_FN.
        script_cmds.append(f"lscores 1-{num_trees} / autest=yes scorefile={AU_TEST_SCORE_FN} replace=yes;")

        paup_script_content = f"#NEXUS\nbegin paup;\n" + "\n".join(script_cmds) + "\nquit;\nend;\n"
        au_cmd_path = self.temp_path / AU_TEST_NEX_FN
        au_cmd_path.write_text(paup_script_content)
        if self.debug: logger.debug(f"AU test PAUP* script ({au_cmd_path}):\n{paup_script_content}")

        try:
            self._run_paup_command_file(AU_TEST_NEX_FN, AU_LOG_FN, timeout_sec=max(1800, 600 * num_trees / 10)) # Dynamic timeout
            return self._parse_au_results(self.temp_path / AU_LOG_FN)
        except Exception as e:
            logger.error(f"AU test execution failed: {e}")
            return None

    def _generate_and_score_constraint_tree(self, clade_taxa: list, tree_idx: int):
        # Returns (relative_tree_filename_str_or_None, likelihood_float_or_None)
        formatted_clade_taxa = [self._format_taxon_for_paup(t) for t in clade_taxa]
        if not formatted_clade_taxa : # Should not happen if called correctly
            logger.warning(f"Constraint {tree_idx}: No taxa provided for clade. Skipping.")
            return None, None

        # All taxa in alignment (already formatted by _format_taxon_for_paup in _convert_to_nexus if that logic was used)
        # For safety, re-format here if needed or assume names are simple.
        # Here, get raw IDs then format.
        all_raw_taxa_ids = [rec.id for rec in self.alignment]
        if len(clade_taxa) == len(all_raw_taxa_ids):
             logger.warning(f"Constraint {tree_idx}: Clade contains all taxa. Skipping as no outgroup possible for MONOPHYLY constraint.")
             return None, None


        clade_spec = "((" + ", ".join(formatted_clade_taxa) + "));"

        constr_tree_fn = f"constraint_tree_{tree_idx}.tre"
        constr_score_fn = f"constraint_score_{tree_idx}.txt"
        constr_cmd_fn = f"constraint_search_{tree_idx}.nex"
        constr_log_fn = f"paup_constraint_{tree_idx}.log"

        script_cmds = [f"execute {NEXUS_ALIGNMENT_FN};", self._get_paup_model_setup_cmds()]
        script_cmds.extend([
            f"constraints clade_constraint (MONOPHYLY) = {clade_spec}",
            "set maxtrees=100 increase=auto;" # Sensible default for constrained search
        ])

        if self.user_paup_block is None: # Standard search
            script_cmds.extend([
                "hsearch start=stepwise addseq=random nreps=1;", # Initial unconstrained to get a tree in memory
                "hsearch start=1 enforce=yes converse=yes constraints=clade_constraint;",
                f"savetrees file={constr_tree_fn} format=newick brlens=yes replace=yes;",
                f"lscores 1 / scorefile={constr_score_fn} replace=yes;"
            ])
        else: # User PAUP block
            block_lower = self.user_paup_block.lower()
            if not any(cmd in block_lower for cmd in ["hsearch", "bandb", "alltrees"]): # If no search specified
                script_cmds.append("hsearch start=stepwise addseq=random nreps=1;")
            # Add enforce to the existing search or a new one. This is tricky.
            # Simplest: add a new constrained search. User might need to adjust their block.
            script_cmds.append("hsearch start=1 enforce=yes converse=yes constraints=clade_constraint;")
            if "savetrees" not in block_lower:
                script_cmds.append(f"savetrees file={constr_tree_fn} format=newick brlens=yes replace=yes;")
            if "lscores" not in block_lower and "lscore" not in block_lower:
                script_cmds.append(f"lscores 1 / scorefile={constr_score_fn} replace=yes;")

        paup_script_content = f"#NEXUS\nbegin paup;\n" + "\n".join(script_cmds) + "\nquit;\nend;\n"
        cmd_file_path = self.temp_path / constr_cmd_fn
        cmd_file_path.write_text(paup_script_content)
        if self.debug: logger.debug(f"Constraint search {tree_idx} script ({cmd_file_path}):\n{paup_script_content}")

        try:
            self._run_paup_command_file(constr_cmd_fn, constr_log_fn, timeout_sec=600)

            score_file_path = self.temp_path / constr_score_fn
            constrained_lnl = self._parse_likelihood_from_score_file(score_file_path)

            tree_file_path = self.temp_path / constr_tree_fn
            if tree_file_path.exists() and tree_file_path.stat().st_size > 0:
                return constr_tree_fn, constrained_lnl # Return relative filename
            else:
                logger.error(f"Constraint tree file {tree_file_path} (idx {tree_idx}) not found or empty.")
                # Try to get LNL from log if score file failed and tree missing
                if constrained_lnl is None:
                    log_content = (self.temp_path / constr_log_fn).read_text()
                    patterns = [r'-ln\s*L\s*=\s*([0-9.]+)', r'likelihood\s*=\s*([0-9.]+)', r'score\s*=\s*([0-9.]+)']
                    for p in patterns:
                        m = re.findall(p, log_content, re.IGNORECASE)
                        if m: constrained_lnl = float(m[-1]); break
                    if constrained_lnl: logger.info(f"Constraint {tree_idx}: LNL from log: {constrained_lnl} (tree file missing)")
                return None, constrained_lnl
        except Exception as e:
            logger.error(f"Constraint tree generation/scoring failed for index {tree_idx}: {e}")
            return None, None

    def calculate_decay_indices(self, perform_site_analysis=False):
        """Calculate ML decay indices for all internal branches of the ML tree."""
        if not self.ml_tree:
            logger.info("ML tree not available. Attempting to build it first...")
            try:
                self.build_ml_tree()
            except Exception as e:
                logger.error(f"Failed to build ML tree during decay calculation: {e}")
                return {} # Cannot proceed

        if not self.ml_tree or self.ml_likelihood is None:
            logger.error("ML tree or its likelihood is missing. Cannot calculate decay indices.")
            return {}

        logger.info("Calculating branch support (decay indices)...")
        all_tree_files_rel = [ML_TREE_FN] # ML tree is first
        constraint_info_map = {} # Maps clade_id_str to its info

        internal_clades = [cl for cl in self.ml_tree.get_nonterminals() if cl and cl.clades] # Biphasic, non-empty
        logger.info(f"ML tree has {len(internal_clades)} internal branches to test.")
        if not internal_clades:
            logger.warning("ML tree has no testable internal branches. No decay indices calculated.")
            return {}

        for i, clade_obj in enumerate(internal_clades):
            clade_log_idx = i + 1 # For filenames and logging (1-based)
            clade_taxa_names = [leaf.name for leaf in clade_obj.get_terminals()]
            total_taxa_count = len(self.ml_tree.get_terminals())

            if len(clade_taxa_names) <= 1 or len(clade_taxa_names) >= total_taxa_count -1:
                logger.info(f"Skipping trivial branch {clade_log_idx} (taxa: {len(clade_taxa_names)}/{total_taxa_count}).")
                continue

            logger.info(f"Processing branch {clade_log_idx}/{len(internal_clades)} (taxa: {len(clade_taxa_names)})")
            rel_constr_tree_fn, constr_lnl = self._generate_and_score_constraint_tree(clade_taxa_names, clade_log_idx)

            if rel_constr_tree_fn: # Successfully generated and scored (even if LNL is None)
                all_tree_files_rel.append(rel_constr_tree_fn)
                clade_id_str = f"Clade_{clade_log_idx}"

                lnl_diff = (constr_lnl - self.ml_likelihood) if constr_lnl is not None and self.ml_likelihood is not None else None
                if constr_lnl is None: logger.warning(f"{clade_id_str}: Constrained LNL is None.")

                constraint_info_map[clade_id_str] = {
                    'taxa': clade_taxa_names,
                    'paup_tree_index': len(all_tree_files_rel), # 1-based index for PAUP*
                    'constrained_lnl': constr_lnl,
                    'lnl_diff': lnl_diff,
                    'tree_filename': rel_constr_tree_fn  # Store tree filename for site analysis
                }
            else:
                logger.warning(f"Failed to generate/score constraint tree for branch {clade_log_idx}. It will be excluded.")

        if not constraint_info_map:
            logger.warning("No valid constraint trees were generated. Skipping AU test.")
            self.decay_indices = {}
            return self.decay_indices

        # Perform site-specific likelihood analysis if requested
        if perform_site_analysis:
            logger.info("Performing site-specific likelihood analysis for each branch...")

            for clade_id, cdata in list(constraint_info_map.items()):
                rel_constr_tree_fn = cdata.get('tree_filename')

                if rel_constr_tree_fn:
                    tree_files = [ML_TREE_FN, rel_constr_tree_fn]
                    site_analysis_result = self._calculate_site_likelihoods(tree_files, clade_id)

                    if site_analysis_result:
                        # Store all site analysis data
                        constraint_info_map[clade_id].update(site_analysis_result)

                        # Log key results
                        supporting = site_analysis_result.get('supporting_sites', 0)
                        conflicting = site_analysis_result.get('conflicting_sites', 0)
                        ratio = site_analysis_result.get('support_ratio', 0.0)
                        weighted = site_analysis_result.get('weighted_support_ratio', 0.0)

                        logger.info(f"Branch {clade_id}: {supporting} supporting sites, {conflicting} conflicting sites, ratio: {ratio:.2f}, weighted ratio: {weighted:.2f}")

        logger.info(f"Running AU test on {len(all_tree_files_rel)} trees (1 ML + {len(constraint_info_map)} constrained).")
        au_test_results = self.run_au_test(all_tree_files_rel)

        self.decay_indices = {}
        # Populate with LNL diffs first, then add AU results
        for cid, cdata in constraint_info_map.items():
            self.decay_indices[cid] = {
                'taxa': cdata['taxa'],
                'lnl_diff': cdata['lnl_diff'],
                'constrained_lnl': cdata['constrained_lnl'],
                'AU_pvalue': None,
                'significant_AU': None
            }

            # Add site analysis data if available
            if 'site_data' in cdata:
                # Copy all the site analysis fields
                for key in ['site_data', 'supporting_sites', 'conflicting_sites', 'neutral_sites',
                           'support_ratio', 'sum_supporting_delta', 'sum_conflicting_delta',
                           'weighted_support_ratio']:
                    if key in cdata:
                        self.decay_indices[cid][key] = cdata[key]

        if au_test_results:
            # Update ML likelihood if AU test scored it differently (should be rare)
            if 1 in au_test_results and self.ml_likelihood is not None:
                if abs(au_test_results[1]['lnL'] - self.ml_likelihood) > 1e-3: # Tolerate small diffs
                    logger.info(f"ML likelihood updated from AU test: {self.ml_likelihood} -> {au_test_results[1]['lnL']}")
                    self.ml_likelihood = au_test_results[1]['lnL']
                    # Need to recalculate all lnl_diffs if ML_LNL changed
                    for cid_recalc in self.decay_indices:
                        constr_lnl_recalc = self.decay_indices[cid_recalc]['constrained_lnl']
                        if constr_lnl_recalc is not None:
                            self.decay_indices[cid_recalc]['lnl_diff'] = constr_lnl_recalc - self.ml_likelihood


            for cid, cdata in constraint_info_map.items():
                paup_idx = cdata['paup_tree_index']
                if paup_idx in au_test_results:
                    au_res_for_tree = au_test_results[paup_idx]
                    self.decay_indices[cid]['AU_pvalue'] = au_res_for_tree['AU_pvalue']
                    if au_res_for_tree['AU_pvalue'] is not None:
                        self.decay_indices[cid]['significant_AU'] = au_res_for_tree['AU_pvalue'] < 0.05

                    # Update constrained LNL from AU test if different
                    current_constr_lnl = self.decay_indices[cid]['constrained_lnl']
                    au_constr_lnl = au_res_for_tree['lnL']
                    if current_constr_lnl is None or abs(current_constr_lnl - au_constr_lnl) > 1e-3:
                        if current_constr_lnl is not None: # Log if it changed significantly
                            logger.info(f"Constrained LNL for {cid} updated by AU test: {current_constr_lnl} -> {au_constr_lnl}")
                        self.decay_indices[cid]['constrained_lnl'] = au_constr_lnl
                        if self.ml_likelihood is not None: # Recalculate diff
                            self.decay_indices[cid]['lnl_diff'] = au_constr_lnl - self.ml_likelihood
                else:
                    logger.warning(f"No AU test result for PAUP tree index {paup_idx} (Clade: {cid}).")
        else:
            logger.warning("AU test failed or returned no results. Decay indices will lack AU p-values.")

        if not self.decay_indices:
            logger.warning("No branch support values were calculated.")
        else:
            logger.info(f"Calculated support values for {len(self.decay_indices)} branches.")

        return self.decay_indices

    def _calculate_site_likelihoods(self, tree_files_list, branch_id):
        """
        Calculate site-specific likelihoods for ML tree vs constrained tree.

        Args:
            tree_files_list: List with [ml_tree_file, constrained_tree_file]
            branch_id: Identifier for the branch being analyzed

        Returns:
            Dictionary with site-specific likelihood differences or None if failed
        """
        if len(tree_files_list) != 2:
            logger.warning(f"Site analysis for branch {branch_id} requires exactly 2 trees (ML and constrained).")
            return None

        site_lnl_file = f"site_lnl_{branch_id}.txt"
        site_script_file = f"site_analysis_{branch_id}.nex"
        site_log_file = f"site_analysis_{branch_id}.log"

        # Create PAUP* script for site likelihood calculation
        script_cmds = [f"execute {NEXUS_ALIGNMENT_FN};", self._get_paup_model_setup_cmds()]

        # Get both trees (ML and constrained)
        script_cmds.append(f"gettrees file={tree_files_list[0]} mode=3 storebrlens=yes;")
        script_cmds.append(f"gettrees file={tree_files_list[1]} mode=7 storebrlens=yes;")

        # Calculate site likelihoods for both trees
        script_cmds.append(f"lscores 1-2 / sitelikes=yes scorefile={site_lnl_file} replace=yes;")

        # Write PAUP* script
        paup_script_content = f"#NEXUS\nbegin paup;\n" + "\n".join(script_cmds) + "\nquit;\nend;\n"
        script_path = self.temp_path / site_script_file
        script_path.write_text(paup_script_content)
        if self.debug:
            logger.debug(f"Site analysis script for {branch_id}:\n{paup_script_content}")

        try:
            # Run PAUP* to calculate site likelihoods
            self._run_paup_command_file(site_script_file, site_log_file, timeout_sec=600)

            # Parse the site likelihood file
            site_lnl_path = self.temp_path / site_lnl_file
            if not site_lnl_path.exists():
                logger.warning(f"Site likelihood file not found for branch {branch_id}.")
                return None

            # Read the site likelihoods file
            site_lnl_content = site_lnl_path.read_text()

            # Initialize dictionaries for tree likelihoods
            tree1_lnl = {}
            tree2_lnl = {}

            # Define patterns to extract data from the file
            # First pattern: Match the header line for each tree section
            tree_header_pattern = r'(\d+)\t([-\d\.]+)\t-\t-'

            # Second pattern: Match site and likelihood lines (indented with tabs)
            site_lnl_pattern = r'\t\t(\d+)\t([-\d\.]+)'

            # Find all tree headers
            tree_headers = list(re.finditer(tree_header_pattern, site_lnl_content))

            # Make sure we found at least 2 tree headers (Tree 1 and Tree 2)
            if len(tree_headers) < 2:
                logger.warning(f"Could not find enough tree headers in site likelihood file for branch {branch_id}")
                if self.debug:
                    logger.debug(f"Site likelihood file content (first 500 chars):\n{site_lnl_content[:500]}...")
                return None

            # Process each tree section
            for i, header_match in enumerate(tree_headers[:2]):  # Only process the first two trees
                tree_num = int(header_match.group(1))

                # If there's a next header, read up to it; otherwise, read to the end
                if i < len(tree_headers) - 1:
                    section_text = site_lnl_content[header_match.end():tree_headers[i+1].start()]
                else:
                    section_text = site_lnl_content[header_match.end():]

                # Find all site and likelihood entries
                site_matches = re.finditer(site_lnl_pattern, section_text)

                # Store data in appropriate dictionary
                for site_match in site_matches:
                    site_num = int(site_match.group(1))
                    lnl_val = float(site_match.group(2))

                    if tree_num == 1:
                        tree1_lnl[site_num] = lnl_val
                    else:
                        tree2_lnl[site_num] = lnl_val

            # Check if we have data for both trees
            if not tree1_lnl:
                logger.warning(f"No data found for Tree 1 in site likelihood file for branch {branch_id}")
                return None

            if not tree2_lnl:
                logger.warning(f"No data found for Tree 2 in site likelihood file for branch {branch_id}")
                return None

            # Create the site_data dictionary with differences
            site_data = {}
            all_sites = sorted(set(tree1_lnl.keys()) & set(tree2_lnl.keys()))

            for site_num in all_sites:
                ml_lnl = tree1_lnl[site_num]
                constrained_lnl = tree2_lnl[site_num]
                delta_lnl = ml_lnl - constrained_lnl

                site_data[site_num] = {
                    'lnL_ML': ml_lnl,
                    'lnL_constrained': constrained_lnl,
                    'delta_lnL': delta_lnl,
                    'supports_branch': delta_lnl < 0  # Negative delta means site supports ML branch
                }

            # Calculate summary statistics
            if site_data:
                deltas = [site_info['delta_lnL'] for site_info in site_data.values()]

                supporting_sites = sum(1 for d in deltas if d < 0)
                conflicting_sites = sum(1 for d in deltas if d > 0)
                neutral_sites = sum(1 for d in deltas if abs(d) < 1e-6)

                # Calculate sum of likelihood differences
                sum_supporting_delta = sum(d for d in deltas if d < 0)  # Sum of negative deltas (supporting)
                sum_conflicting_delta = sum(d for d in deltas if d > 0)  # Sum of positive deltas (conflicting)

                # Calculate weighted support ratio
                weighted_support_ratio = abs(sum_supporting_delta) / sum_conflicting_delta if sum_conflicting_delta > 0 else float('inf')

                # Calculate standard support ratio
                support_ratio = supporting_sites / conflicting_sites if conflicting_sites > 0 else float('inf')

                logger.info(f"Extracted site likelihoods for {len(site_data)} sites for branch {branch_id}")
                logger.info(f"Branch {branch_id}: {supporting_sites} supporting sites, {conflicting_sites} conflicting sites")
                logger.info(f"Branch {branch_id}: {supporting_sites} supporting sites, {conflicting_sites} conflicting sites, ratio: {support_ratio:.2f}")
                logger.info(f"Branch {branch_id}: Sum supporting delta: {sum_supporting_delta:.4f}, sum conflicting: {sum_conflicting_delta:.4f}, weighted ratio: {weighted_support_ratio:.2f}")

                # Return a comprehensive dictionary with all info
                return {
                    'site_data': site_data,
                    'supporting_sites': supporting_sites,
                    'conflicting_sites': conflicting_sites,
                    'neutral_sites': neutral_sites,
                    'support_ratio': support_ratio,
                    'sum_supporting_delta': sum_supporting_delta,
                    'sum_conflicting_delta': sum_conflicting_delta,
                    'weighted_support_ratio': weighted_support_ratio
                }
            else:
                logger.warning(f"No comparable site likelihoods found for branch {branch_id}")
                return None

        except Exception as e:
            logger.error(f"Failed to calculate site likelihoods for branch {branch_id}: {e}")
            if self.debug:
                import traceback
                logger.debug(f"Traceback for site likelihood calculation error:\n{traceback.format_exc()}")
            return None

    def annotate_trees(self, output_dir: Path, base_filename: str = "annotated_tree"):
        """
        Create annotated trees with different support values:
        1. A tree with AU p-values as branch labels
        2. A tree with log-likelihood differences as branch labels
        3. A combined tree with both values as FigTree-compatible branch labels
        4. A tree with bootstrap values if bootstrap analysis was performed
        5. A comprehensive tree with bootstrap, AU, and LnL values if bootstrap was performed

        Args:
            output_dir: Directory to save the tree files
            base_filename: Base name for the tree files (without extension)

        Returns:
            Dict with paths to the created tree files
        """
        if not self.ml_tree or not self.decay_indices:
            logger.warning("ML tree or decay indices missing. Cannot annotate trees.")
            return {}

        output_dir.mkdir(parents=True, exist_ok=True)
        tree_files = {}

        try:
            # Create AU p-value annotated tree
            au_tree_path = output_dir / f"{base_filename}_au.nwk"
            try:
                # Work on a copy to avoid modifying self.ml_tree
                temp_tree_for_au = self.temp_path / f"ml_tree_for_au_annotation.nwk"
                Phylo.write(self.ml_tree, str(temp_tree_for_au), "newick")
                cleaned_tree_path = self._clean_newick_tree(temp_tree_for_au)
                au_tree = Phylo.read(str(cleaned_tree_path), "newick")

                annotated_nodes_count = 0
                for node in au_tree.get_nonterminals():
                    if not node or not node.clades: continue
                    node_taxa_set = set(leaf.name for leaf in node.get_terminals())

                    # Find matching entry in decay_indices by taxa set
                    matched_data = None
                    for decay_id_str, decay_info in self.decay_indices.items():
                        if 'taxa' in decay_info and set(decay_info['taxa']) == node_taxa_set:
                            matched_data = decay_info
                            break

                    node.confidence = None  # Default
                    if matched_data and 'AU_pvalue' in matched_data and matched_data['AU_pvalue'] is not None:
                        node.confidence = float(matched_data['AU_pvalue'])
                        annotated_nodes_count += 1

                Phylo.write(au_tree, str(au_tree_path), "newick")
                logger.info(f"Annotated tree with {annotated_nodes_count} branch values written to {au_tree_path} (type: au).")
                tree_files['au'] = au_tree_path
            except Exception as e:
                logger.error(f"Failed to create AU tree: {e}")

            # Create log-likelihood difference annotated tree
            lnl_tree_path = output_dir / f"{base_filename}_lnl.nwk"
            try:
                temp_tree_for_lnl = self.temp_path / f"ml_tree_for_lnl_annotation.nwk"
                Phylo.write(self.ml_tree, str(temp_tree_for_lnl), "newick")
                cleaned_tree_path = self._clean_newick_tree(temp_tree_for_lnl)
                lnl_tree = Phylo.read(str(cleaned_tree_path), "newick")

                annotated_nodes_count = 0
                for node in lnl_tree.get_nonterminals():
                    if not node or not node.clades: continue
                    node_taxa_set = set(leaf.name for leaf in node.get_terminals())

                    matched_data = None
                    for decay_id_str, decay_info in self.decay_indices.items():
                        if 'taxa' in decay_info and set(decay_info['taxa']) == node_taxa_set:
                            matched_data = decay_info
                            break

                    node.confidence = None  # Default
                    if matched_data and 'lnl_diff' in matched_data and matched_data['lnl_diff'] is not None:
                        node.confidence = abs(matched_data['lnl_diff'])
                        annotated_nodes_count += 1

                Phylo.write(lnl_tree, str(lnl_tree_path), "newick")
                logger.info(f"Annotated tree with {annotated_nodes_count} branch values written to {lnl_tree_path} (type: lnl).")
                tree_files['lnl'] = lnl_tree_path
            except Exception as e:
                logger.error(f"Failed to create LNL tree: {e}")

            # Create combined annotation tree manually for FigTree
            combined_tree_path = output_dir / f"{base_filename}_combined.nwk"
            try:
                # For the combined approach, we'll directly modify the Newick string
                # First, get both trees as strings
                temp_tree_for_combined = self.temp_path / f"ml_tree_for_combined_annotation.nwk"
                Phylo.write(self.ml_tree, str(temp_tree_for_combined), "newick")

                # Create a mapping from node taxa sets to combined annotation strings
                node_annotations = {}

                # If bootstrap analysis was performed, get bootstrap values first
                bootstrap_values = {}
                if hasattr(self, 'bootstrap_tree') and self.bootstrap_tree:
                    for node in self.bootstrap_tree.get_nonterminals():
                        if node.confidence is not None:
                            taxa_set = frozenset(leaf.name for leaf in node.get_terminals())
                            bootstrap_values[taxa_set] = node.confidence

                for node in self.ml_tree.get_nonterminals():
                    if not node or not node.clades: continue
                    node_taxa_set = frozenset(leaf.name for leaf in node.get_terminals())

                    # Initialize annotation parts
                    annotation_parts = []

                    # Add bootstrap value if available
                    if bootstrap_values and node_taxa_set in bootstrap_values:
                        bs_val = bootstrap_values[node_taxa_set]
                        annotation_parts.append(f"BS:{int(bs_val)}")

                    # Add AU and LnL values if available
                    for decay_id_str, decay_info in self.decay_indices.items():
                        if 'taxa' in decay_info and frozenset(decay_info['taxa']) == node_taxa_set:
                            au_val = decay_info.get('AU_pvalue')
                            lnl_val = decay_info.get('lnl_diff')

                            if au_val is not None:
                                annotation_parts.append(f"AU:{au_val:.4f}")

                            if lnl_val is not None:
                                annotation_parts.append(f"LnL:{abs(lnl_val):.4f}")
                            break

                    # Only add to annotations if we have at least one value
                    if annotation_parts:
                        node_annotations[node_taxa_set] = "|".join(annotation_parts)

                # Now, we'll manually construct a combined Newick string
                # Read the base tree without annotations
                base_tree_str = temp_tree_for_combined.read_text()

                # We'll create a combined tree by using string replacement on the base tree
                # First, make a working copy of the ML tree
                cleaned_tree_path = self._clean_newick_tree(temp_tree_for_combined)
                combined_tree = Phylo.read(str(cleaned_tree_path), "newick")

                # Add our custom annotations
                annotated_nodes_count = 0
                for node in combined_tree.get_nonterminals():
                    if not node or not node.clades: continue
                    node_taxa_set = frozenset(leaf.name for leaf in node.get_terminals())

                    if node_taxa_set in node_annotations:
                        # We need to use string values for combined annotation
                        # Save our combined annotation as a string in .name instead of .confidence
                        # This is a hack that works with some tree viewers including FigTree
                        node.name = node_annotations[node_taxa_set]
                        annotated_nodes_count += 1

                # Write the modified tree
                Phylo.write(combined_tree, str(combined_tree_path), "newick")

                logger.info(f"Annotated tree with {annotated_nodes_count} branch values written to {combined_tree_path} (type: combined).")
                tree_files['combined'] = combined_tree_path
            except Exception as e:
                logger.error(f"Failed to create combined tree: {e}")
                import traceback
                logger.debug(f"Traceback: {traceback.format_exc()}")

            # Handle bootstrap tree if bootstrap analysis was performed
            if hasattr(self, 'bootstrap_tree') and self.bootstrap_tree:
                # 1. Save the bootstrap tree directly
                bootstrap_tree_path = output_dir / f"{base_filename}_bootstrap.nwk"
                try:
                    Phylo.write(self.bootstrap_tree, str(bootstrap_tree_path), "newick")
                    logger.info(f"Bootstrap tree written to {bootstrap_tree_path}")
                    tree_files['bootstrap'] = bootstrap_tree_path
                except Exception as e:
                    logger.error(f"Failed to write bootstrap tree: {e}")

                # 2. Create a comprehensive tree with bootstrap, AU and LnL values
                comprehensive_tree_path = output_dir / f"{base_filename}_comprehensive.nwk"
                try:
                    temp_tree_for_comprehensive = self.temp_path / f"ml_tree_for_comprehensive_annotation.nwk"
                    Phylo.write(self.ml_tree, str(temp_tree_for_comprehensive), "newick")
                    cleaned_tree_path = self._clean_newick_tree(temp_tree_for_comprehensive)
                    comprehensive_tree = Phylo.read(str(cleaned_tree_path), "newick")

                    # Get bootstrap values for each clade
                    bootstrap_values = {}
                    for node in self.bootstrap_tree.get_nonterminals():
                        if node.confidence is not None:
                            taxa_set = frozenset(leaf.name for leaf in node.get_terminals())
                            bootstrap_values[taxa_set] = node.confidence

                    # Create comprehensive annotations
                    node_annotations = {}
                    for node in self.ml_tree.get_nonterminals():
                        if not node or not node.clades: continue
                        node_taxa_set = frozenset(leaf.name for leaf in node.get_terminals())

                        # Find matching decay info
                        matched_data = None
                        for decay_id_str, decay_info in self.decay_indices.items():
                            if 'taxa' in decay_info and frozenset(decay_info['taxa']) == node_taxa_set:
                                matched_data = decay_info
                                break

                        # Combine all values
                        annotation_parts = []

                        # Add bootstrap value if available
                        if node_taxa_set in bootstrap_values:
                            bs_val = bootstrap_values[node_taxa_set]
                            annotation_parts.append(f"BS:{int(bs_val)}")

                        # Add AU and LnL values if available
                        if matched_data:
                            au_val = matched_data.get('AU_pvalue')
                            lnl_val = matched_data.get('lnl_diff')

                            if au_val is not None:
                                annotation_parts.append(f"AU:{au_val:.4f}")

                            if lnl_val is not None:
                                annotation_parts.append(f"LnL:{abs(lnl_val):.4f}")

                        if annotation_parts:
                            node_annotations[node_taxa_set] = "|".join(annotation_parts)

                    # Apply annotations to tree
                    annotated_nodes_count = 0
                    for node in comprehensive_tree.get_nonterminals():
                        if not node or not node.clades: continue
                        node_taxa_set = frozenset(leaf.name for leaf in node.get_terminals())

                        if node_taxa_set in node_annotations:
                            node.name = node_annotations[node_taxa_set]
                            annotated_nodes_count += 1

                    # Write the tree
                    Phylo.write(comprehensive_tree, str(comprehensive_tree_path), "newick")
                    logger.info(f"Comprehensive tree with {annotated_nodes_count} branch values written to {comprehensive_tree_path}")
                    tree_files['comprehensive'] = comprehensive_tree_path
                except Exception as e:
                    logger.error(f"Failed to create comprehensive tree: {e}")
                    if self.debug:
                        import traceback
                        logger.debug(f"Traceback: {traceback.format_exc()}")

            return tree_files

        except Exception as e:
            logger.error(f"Failed to annotate trees: {e}")
            if hasattr(self, 'debug') and self.debug:
                import traceback
                logger.debug(f"Traceback: {traceback.format_exc()}")
            return tree_files  # Return any successfully created files

    def write_results(self, output_path: Path):
        if not self.decay_indices:
            logger.warning("No branch support results to write.")
            # Create an empty or minimal file? For now, just return.
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with output_path.open('w') as f:
                    f.write("No branch support results calculated.\n")
                    if self.ml_likelihood is not None:
                        f.write(f"ML tree log-likelihood: {self.ml_likelihood:.6f}\n")
                return
            except Exception as e_write:
                logger.error(f"Failed to write empty results file {output_path}: {e_write}")
                return

        # Check if bootstrap analysis was performed
        has_bootstrap = hasattr(self, 'bootstrap_tree') and self.bootstrap_tree

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open('w') as f:
            f.write("MLDecay Branch Support Analysis\n")
            f.write("=" * 30 + "\n\n")
            ml_l = self.ml_likelihood if self.ml_likelihood is not None else "N/A"
            if isinstance(ml_l, float): ml_l = f"{ml_l:.6f}"
            f.write(f"ML tree log-likelihood: {ml_l}\n\n")
            f.write("Branch Support Values:\n")
            f.write("-" * 80 + "\n")

            # Header - add bootstrap column if bootstrap analysis was performed
            header = "Clade_ID\tNum_Taxa\tConstrained_lnL\tLnL_Diff_from_ML\tAU_p-value\tSignificant_AU (p<0.05)"
            if has_bootstrap:
                header += "\tBootstrap"
            header += "\tTaxa_List\n"
            f.write(header)

            # Create mapping of taxa sets to bootstrap values if bootstrap analysis was performed
            bootstrap_values = {}
            if has_bootstrap:
                for node in self.bootstrap_tree.get_nonterminals():
                    if node.confidence is not None:
                        taxa_set = frozenset(leaf.name for leaf in node.get_terminals())
                        bootstrap_values[taxa_set] = node.confidence

            for clade_id, data in sorted(self.decay_indices.items()): # Sort for consistent output
                taxa_list = sorted(data.get('taxa', []))
                taxa_str = ",".join(taxa_list)
                num_taxa = len(taxa_list)

                c_lnl = data.get('constrained_lnl', 'N/A')
                if isinstance(c_lnl, float): c_lnl = f"{c_lnl:.4f}"

                lnl_d = data.get('lnl_diff', 'N/A')
                if isinstance(lnl_d, float): lnl_d = f"{lnl_d:.4f}"

                au_p = data.get('AU_pvalue', 'N/A')
                if isinstance(au_p, float): au_p = f"{au_p:.4f}"

                sig_au = data.get('significant_AU', 'N/A')
                if isinstance(sig_au, bool): sig_au = "Yes" if sig_au else "No"

                row = f"{clade_id}\t{num_taxa}\t{c_lnl}\t{lnl_d}\t{au_p}\t{sig_au}"

                # Add bootstrap value if available
                if has_bootstrap:
                    taxa_set = frozenset(taxa_list)
                    bs_val = bootstrap_values.get(taxa_set, "N/A")
                    if isinstance(bs_val, (int, float)):
                        bs_val = f"{int(bs_val)}"
                    row += f"\t{bs_val}"

                row += f"\t{taxa_str}\n"
                f.write(row)

        logger.info(f"Results written to {output_path}")

    def generate_detailed_report(self, output_path: Path):
        # Basic check
        if not self.decay_indices and self.ml_likelihood is None:
            logger.warning("No results available to generate detailed report.")
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with output_path.open('w') as f: f.write("# ML-Decay Report\n\nNo analysis results to report.\n")
                return
            except Exception as e_write:
                 logger.error(f"Failed to write empty detailed report {output_path}: {e_write}")
                 return

        # Check if bootstrap analysis was performed
        has_bootstrap = hasattr(self, 'bootstrap_tree') and self.bootstrap_tree

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open('w') as f:
            f.write(f"# ML-Decay Branch Support Analysis Report (v{VERSION})\n\n")
            f.write(f"Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

            f.write("## Analysis Parameters\n\n")
            f.write(f"- Alignment file: `{self.alignment_file.name}`\n")
            f.write(f"- Data type: `{self.data_type}`\n")
            if self.user_paup_block:
                f.write("- Model: User-defined PAUP* block\n")
            else:
                f.write(f"- Model string: `{self.model_str}`\n")
                f.write(f"- PAUP* `lset` command: `{self.paup_model_cmds}`\n")
            if has_bootstrap:
                f.write("- Bootstrap analysis: Performed\n")

            ml_l = self.ml_likelihood if self.ml_likelihood is not None else "N/A"
            if isinstance(ml_l, float): ml_l = f"{ml_l:.6f}"
            f.write("\n## Summary Statistics\n\n")
            f.write(f"- ML tree log-likelihood: **{ml_l}**\n")
            f.write(f"- Number of internal branches tested: {len(self.decay_indices)}\n")

            if self.decay_indices:
                lnl_diffs = [d['lnl_diff'] for d in self.decay_indices.values() if d.get('lnl_diff') is not None]
                if lnl_diffs:
                    f.write(f"- Avg log-likelihood difference (constrained vs ML): {np.mean(lnl_diffs):.4f}\n")
                    f.write(f"- Min log-likelihood difference: {min(lnl_diffs):.4f}\n")
                    f.write(f"- Max log-likelihood difference: {max(lnl_diffs):.4f}\n")

                au_pvals = [d['AU_pvalue'] for d in self.decay_indices.values() if d.get('AU_pvalue') is not None]
                if au_pvals:
                    sig_au_count = sum(1 for p in au_pvals if p < 0.05)
                    f.write(f"- Branches with significant AU support (p < 0.05): {sig_au_count} / {len(au_pvals)} evaluated\n")

            f.write("\n## Detailed Branch Support Results\n\n")

            # Table header - add bootstrap column if needed
            header = "| Clade ID | Taxa Count | Constrained lnL | LnL Diff from ML | AU p-value | Significant (AU) "
            if has_bootstrap:
                header += "| Bootstrap "
            header += "| Included Taxa (sample) |\n"
            f.write(header)

            # Table separator - add extra cell for bootstrap if needed
            separator = "|----------|------------|-----------------|------------------|------------|-------------------- "
            if has_bootstrap:
                separator += "|----------- "
            separator += "|--------------------------|\n"
            f.write(separator)

            # Get bootstrap values if bootstrap analysis was performed
            bootstrap_values = {}
            if has_bootstrap:
                for node in self.bootstrap_tree.get_nonterminals():
                    if node.confidence is not None:
                        taxa_set = frozenset(leaf.name for leaf in node.get_terminals())
                        bootstrap_values[taxa_set] = node.confidence

            for clade_id, data in sorted(self.decay_indices.items()):
                taxa_list = sorted(data.get('taxa', []))
                taxa_count = len(taxa_list)
                taxa_sample = ", ".join(taxa_list[:3]) + ('...' if taxa_count > 3 else '')

                c_lnl = data.get('constrained_lnl', 'N/A')
                if isinstance(c_lnl, float): c_lnl = f"{c_lnl:.4f}"

                lnl_d = data.get('lnl_diff', 'N/A')
                if isinstance(lnl_d, float): lnl_d = f"{lnl_d:.4f}"

                au_p = data.get('AU_pvalue', 'N/A')
                if isinstance(au_p, float): au_p = f"{au_p:.4f}"

                sig_au = data.get('significant_AU', 'N/A')
                if isinstance(sig_au, bool): sig_au = "**Yes**" if sig_au else "No"

                # Build the table row
                row = f"| {clade_id} | {taxa_count} | {c_lnl} | {lnl_d} | {au_p} | {sig_au} "

                # Add bootstrap column if available
                if has_bootstrap:
                    taxa_set = frozenset(taxa_list)
                    bs_val = bootstrap_values.get(taxa_set, "N/A")
                    if isinstance(bs_val, (int, float)):
                        bs_val = f"{int(bs_val)}"
                    row += f"| {bs_val} "

                row += f"| {taxa_sample} |\n"
                f.write(row)

            f.write("\n## Interpretation Guide\n\n")
            f.write("- **LnL Diff from ML**: Log-likelihood of the best tree *without* the clade minus ML tree's log-likelihood. More negative (larger absolute difference) implies stronger support for the clade's presence in the ML tree.\n")
            f.write("- **AU p-value**: P-value from the Approximately Unbiased test comparing the ML tree against the alternative (constrained) tree. Lower p-values (e.g., < 0.05) suggest the alternative tree (where the clade is broken) is significantly worse than the ML tree, thus supporting the clade.\n")
            if has_bootstrap:
                f.write("- **Bootstrap**: Bootstrap support value (percentage of bootstrap replicates in which the clade appears). Higher values (e.g., > 70) suggest stronger support for the clade.\n")
        logger.info(f"Detailed report written to {output_path}")

    def write_site_analysis_results(self, output_dir: Path, keep_tree_files=False):
        """
        Write site-specific likelihood analysis results to files.

        Args:
            output_dir: Directory to save the site analysis files
            keep_tree_files: Whether to keep the Newick files used for HTML visualization
        """
        if not self.decay_indices:
            logger.warning("No decay indices available for site analysis output.")
            return

        # Check if any clade has site data
        has_site_data = any('site_data' in data for data in self.decay_indices.values())
        if not has_site_data:
            logger.warning("No site-specific analysis data available to write.")
            return

        output_dir.mkdir(parents=True, exist_ok=True)

        # Create a summary file for all branches
        summary_path = output_dir / "site_analysis_summary.txt"
        with summary_path.open('w') as f:
            f.write("Branch Site Analysis Summary\n")
            f.write("=========================\n\n")
            f.write("Clade_ID\tSupporting_Sites\tConflicting_Sites\tNeutral_Sites\tSupport_Ratio\tSum_Supporting_Delta\tSum_Conflicting_Delta\tWeighted_Support_Ratio\n")

            for clade_id, data in sorted(self.decay_indices.items()):
                if 'site_data' not in data:
                    continue

                supporting = data.get('supporting_sites', 0)
                conflicting = data.get('conflicting_sites', 0)
                neutral = data.get('neutral_sites', 0)
                ratio = data.get('support_ratio', 0.0)
                sum_supporting = data.get('sum_supporting_delta', 0.0)
                sum_conflicting = data.get('sum_conflicting_delta', 0.0)
                weighted_ratio = data.get('weighted_support_ratio', 0.0)

                if ratio == float('inf'):
                    ratio_str = "Inf"
                else:
                    ratio_str = f"{ratio:.4f}"

                if weighted_ratio == float('inf'):
                    weighted_ratio_str = "Inf"
                else:
                    weighted_ratio_str = f"{weighted_ratio:.4f}"

                f.write(f"{clade_id}\t{supporting}\t{conflicting}\t{neutral}\t{ratio_str}\t{sum_supporting:.4f}\t{sum_conflicting:.4f}\t{weighted_ratio_str}\n")

        logger.info(f"Site analysis summary written to {summary_path}")

        # For each branch, write detailed site data
        for clade_id, data in self.decay_indices.items():
            if 'site_data' not in data:
                continue

            site_data_path = output_dir / f"site_data_{clade_id}.txt"
            with site_data_path.open('w') as f:
                f.write(f"Site-Specific Likelihood Analysis for {clade_id}\n")
                f.write("=" * 50 + "\n\n")
                f.write(f"Supporting sites: {data.get('supporting_sites', 0)}\n")
                f.write(f"Conflicting sites: {data.get('conflicting_sites', 0)}\n")
                f.write(f"Neutral sites: {data.get('neutral_sites', 0)}\n")
                f.write(f"Support ratio: {data.get('support_ratio', 0.0):.4f}\n")
                f.write(f"Sum of supporting deltas: {data.get('sum_supporting_delta', 0.0):.4f}\n")
                f.write(f"Sum of conflicting deltas: {data.get('sum_conflicting_delta', 0.0):.4f}\n")
                f.write(f"Weighted support ratio: {data.get('weighted_support_ratio', 0.0):.4f}\n\n")
                f.write("Site\tML_Tree_lnL\tConstrained_lnL\tDelta_lnL\tSupports_Branch\n")

                # Make sure site_data is a dictionary with entries for each site
                site_data = data.get('site_data', {})
                if isinstance(site_data, dict) and site_data:
                    for site_num, site_info in sorted(site_data.items()):
                        # Safely access each field with a default
                        ml_lnl = site_info.get('lnL_ML', 0.0)
                        constrained_lnl = site_info.get('lnL_constrained', 0.0)
                        delta_lnl = site_info.get('delta_lnL', 0.0)
                        supports = site_info.get('supports_branch', False)

                        f.write(f"{site_num}\t{ml_lnl:.6f}\t{constrained_lnl:.6f}\t{delta_lnl:.6f}\t{supports}\n")

            logger.info(f"Detailed site data for {clade_id} written to {site_data_path}")

        # Generate site analysis visualizations
        try:
            import matplotlib.pyplot as plt
            import seaborn as sns
            import numpy as np

            # Get visualization options
            viz_format = getattr(self, 'viz_format', 'png')
            generate_html = getattr(self, 'generate_html', True)
            js_cdn = getattr(self, 'js_cdn', True)

            for clade_id, data in self.decay_indices.items():
                if 'site_data' not in data:
                    continue

                # Extract data for plotting
                site_data = data.get('site_data', {})
                if not site_data:
                    continue

                site_nums = sorted(site_data.keys())
                deltas = [site_data[site]['delta_lnL'] for site in site_nums if 'delta_lnL' in site_data[site]]

                if not deltas:
                    continue

                # Get taxa in this clade for visualization
                clade_taxa = data.get('taxa', [])

                # Prepare taxa list for title display
                if len(clade_taxa) <= 3:
                    taxa_display = ", ".join(clade_taxa)
                else:
                    taxa_display = f"{', '.join(sorted(clade_taxa)[:3])}... (+{len(clade_taxa)-3} more)"

                # Create standard site analysis plot
                fig = plt.figure(figsize=(12, 6))
                ax_main = fig.add_subplot(111)

                # Create the main bar plot
                bar_colors = ['green' if d < 0 else 'red' for d in deltas]
                ax_main.bar(range(len(deltas)), deltas, color=bar_colors, alpha=0.7)

                # Add x-axis ticks at reasonable intervals
                if len(site_nums) > 50:
                    tick_interval = max(1, len(site_nums) // 20)
                    tick_positions = range(0, len(site_nums), tick_interval)
                    tick_labels = [site_nums[i] for i in tick_positions if i < len(site_nums)]
                    ax_main.set_xticks(tick_positions)
                    ax_main.set_xticklabels(tick_labels, rotation=45)
                else:
                    ax_main.set_xticks(range(len(site_nums)))
                    ax_main.set_xticklabels(site_nums, rotation=45)

                # Add reference line at y=0
                ax_main.axhline(y=0, color='black', linestyle='-', alpha=0.3)

                # Add title that includes some taxa information
                ax_main.set_title(f"Site-Specific Likelihood Differences for {clade_id} ({taxa_display})")
                ax_main.set_xlabel("Site Position")
                ax_main.set_ylabel("Delta lnL (ML - Constrained)")

                # Add summary info text box
                support_ratio = data.get('support_ratio', 0.0)
                weighted_ratio = data.get('weighted_support_ratio', 0.0)

                ratio_text = "Inf" if support_ratio == float('inf') else f"{support_ratio:.2f}"
                weighted_text = "Inf" if weighted_ratio == float('inf') else f"{weighted_ratio:.2f}"

                info_text = (
                    f"Supporting sites: {data.get('supporting_sites', 0)}\n"
                    f"Conflicting sites: {data.get('conflicting_sites', 0)}\n"
                    f"Support ratio: {ratio_text}\n"
                    f"Weighted ratio: {weighted_text}"
                )

                # Add text box with summary info
                ax_main.text(
                    0.02, 0.95, info_text,
                    transform=ax_main.transAxes,
                    bbox=dict(boxstyle='round', facecolor='white', alpha=0.8)
                )

                plt.tight_layout()

                # Save plot in the requested format
                plot_path = output_dir / f"site_plot_{clade_id}.{viz_format}"
                plt.savefig(str(plot_path), dpi=150, format=viz_format)
                plt.close(fig)

                logger.info(f"Site-specific likelihood plot for {clade_id} saved to {plot_path}")

                # Optional: Create a histogram of delta values
                plt.figure(figsize=(10, 5))
                sns.histplot(deltas, kde=True, bins=30)
                plt.axvline(x=0, color='black', linestyle='--')
                plt.title(f"Distribution of Site Likelihood Differences for {clade_id}")
                plt.xlabel("Delta lnL (ML - Constrained)")
                plt.tight_layout()

                hist_path = output_dir / f"site_hist_{clade_id}.{viz_format}"
                plt.savefig(str(hist_path), dpi=150, format=viz_format)
                plt.close()

                logger.info(f"Site likelihood histogram for {clade_id} saved to {hist_path}")

                # Create interactive HTML tree visualization if enabled
                if generate_html and clade_taxa:
                    # Create HTML tree visualization
                    html_path = self.create_interactive_tree_html(output_dir, clade_id, clade_taxa)
                    if html_path:
                        logger.info(f"Interactive tree visualization for {clade_id} created at {html_path}")

                if not keep_tree_files and not self.debug and not self.keep_files:
                    for file_path in output_dir.glob("tree_*.nwk"):
                        try:
                            file_path.unlink()
                            logger.debug(f"Deleted tree file for HTML: {file_path}")
                        except Exception as e:
                            logger.warning(f"Failed to delete tree file {file_path}: {e}")

        except ImportError:
            logger.warning("Matplotlib/seaborn not available for site analysis visualization.")
        except Exception as e:
            logger.error(f"Error creating site analysis visualizations: {e}")
            if self.debug:
                import traceback
                logger.debug(f"Visualization error traceback: {traceback.format_exc()}")

    def visualize_support_distribution(self, output_path: Path, value_type="au", **kwargs):
        if not self.decay_indices: logger.warning("No data for support distribution plot."); return
        try:
            import matplotlib.pyplot as plt
            import seaborn as sns # numpy is usually a dependency of seaborn/matplotlib

            vals = []
            for data in self.decay_indices.values():
                if value_type == "au" and data.get('AU_pvalue') is not None: vals.append(data['AU_pvalue'])
                elif value_type == "lnl" and data.get('lnl_diff') is not None: vals.append(abs(data['lnl_diff']))
            if not vals: logger.warning(f"No '{value_type}' values for distribution plot."); return

            plt.figure(figsize=(kwargs.get('width',10), kwargs.get('height',6)))
            sns.histplot(vals, kde=True)
            title, xlabel = "", ""
            if value_type == "au":
                plt.axvline(0.05, color='r', linestyle='--', label='p=0.05 threshold')
                title, xlabel = 'Distribution of AU Test p-values', 'AU p-value'
            else: # lnl
                mean_val = np.mean(vals)
                plt.axvline(mean_val, color='g', linestyle='--', label=f'Mean diff ({mean_val:.2f})')
                title, xlabel = 'Distribution of abs(Log-Likelihood Differences)', 'abs(LNL Difference)'
            plt.title(title); plt.xlabel(xlabel); plt.ylabel('Frequency'); plt.legend(); plt.tight_layout()

            output_path.parent.mkdir(parents=True, exist_ok=True)
            plt.savefig(str(output_path), format=kwargs.get('format',"png"), dpi=300); plt.close()
            logger.info(f"Support distribution plot saved to {output_path}")
        except ImportError: logger.error("Matplotlib/Seaborn not found for visualization.")
        except Exception as e: logger.error(f"Failed support distribution plot: {e}")

    def create_interactive_tree_html(self, output_dir, clade_id, highlight_taxa):
        """
        Create an interactive HTML tree visualization for a specific clade.

        Args:
            output_dir: Directory to save the HTML file
            clade_id: Identifier for the clade
            highlight_taxa: List of taxa names to highlight in the tree

        Returns:
            Path to the created HTML file or None if creation failed
        """
        try:
            import json
            from Bio import Phylo

            # Ensure output directory exists
            output_dir.mkdir(parents=True, exist_ok=True)

            # Output filenames
            html_path = output_dir / f"tree_{clade_id}.html"
            tree_path = output_dir / f"tree_{clade_id}.nwk"

            # Write the ML tree to a Newick file (needed for the HTML to load)
            Phylo.write(self.ml_tree, str(tree_path), "newick")

            # Clean up the tree file if needed
            cleaned_tree_path = self._clean_newick_tree(tree_path)

            # Get tree statistics
            total_taxa = len(self.ml_tree.get_terminals())
            highlight_ratio = len(highlight_taxa) / total_taxa if total_taxa > 0 else 0

            # Get site analysis data if available
            site_data = None
            if hasattr(self, 'decay_indices') and clade_id in self.decay_indices:
                clade_data = self.decay_indices[clade_id]
                if 'site_data' in clade_data:
                    supporting = clade_data.get('supporting_sites', 0)
                    conflicting = clade_data.get('conflicting_sites', 0)
                    support_ratio = clade_data.get('support_ratio', None)
                    if support_ratio == float('inf'):
                        support_ratio_str = "Infinity"
                    elif support_ratio is not None:
                        support_ratio_str = f"{support_ratio:.2f}"
                    else:
                        support_ratio_str = "N/A"

                    weighted_ratio = clade_data.get('weighted_support_ratio', None)
                    if weighted_ratio == float('inf'):
                        weighted_ratio_str = "Infinity"
                    elif weighted_ratio is not None:
                        weighted_ratio_str = f"{weighted_ratio:.2f}"
                    else:
                        weighted_ratio_str = "N/A"

                    site_data = {
                        'supporting': supporting,
                        'conflicting': conflicting,
                        'support_ratio': support_ratio_str,
                        'weighted_ratio': weighted_ratio_str
                    }

            # Get AU test and likelihood data
            au_data = None
            if hasattr(self, 'decay_indices') and clade_id in self.decay_indices:
                clade_data = self.decay_indices[clade_id]
                au_pvalue = clade_data.get('AU_pvalue', None)
                lnl_diff = clade_data.get('lnl_diff', None)

                if au_pvalue is not None or lnl_diff is not None:
                    au_data = {
                        'au_pvalue': f"{au_pvalue:.4f}" if au_pvalue is not None else "N/A",
                        'lnl_diff': f"{lnl_diff:.4f}" if lnl_diff is not None else "N/A",
                        'significant': clade_data.get('significant_AU', False)
                    }

            # Get bootstrap data if available
            bootstrap_value = None
            if hasattr(self, 'bootstrap_tree') and self.bootstrap_tree:
                # Find the corresponding node in the bootstrap tree
                for node in self.bootstrap_tree.get_nonterminals():
                    node_taxa = set(leaf.name for leaf in node.get_terminals())
                    if node_taxa == set(highlight_taxa):
                        bootstrap_value = int(node.confidence) if node.confidence is not None else None
                        break

            # Format taxa for title display
            if len(highlight_taxa) <= 5:
                taxa_display = ", ".join(highlight_taxa)
            else:
                taxa_display = f"{', '.join(sorted(highlight_taxa)[:5])}... (+{len(highlight_taxa)-5} more)"

            # Start building HTML content in parts to avoid f-string backslash issues
            # Basic structure
            html_parts = []

            # Header
            html_parts.append("<!DOCTYPE html>")
            html_parts.append("<html lang=\"en\">")
            html_parts.append("<head>")
            html_parts.append("    <meta charset=\"UTF-8\">")
            html_parts.append("    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">")
            html_parts.append(f"    <title>MLDecay - Interactive Tree for {clade_id}</title>")

            # CSS
            html_parts.append("""    <style>
            body {
                font-family: Arial, sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 1200px;
                margin: 0 auto;
                padding: 20px;
            }
            h1, h2, h3 {
                color: #2c3e50;
            }
            .container {
                display: flex;
                flex-wrap: wrap;
                gap: 20px;
            }
            .tree-section {
                flex: 1;
                min-width: 500px;
            }
            .info-section {
                flex: 1;
                min-width: 300px;
                background-color: #f8f9fa;
                padding: 15px;
                border-radius: 5px;
            }
            #tree_container {
                width: 100%;
                height: 600px;
                border: 1px solid #ddd;
                border-radius: 4px;
                overflow: hidden;
            }
            .table {
                width: 100%;
                border-collapse: collapse;
                margin-bottom: 15px;
            }
            .table th, .table td {
                padding: 8px;
                text-align: left;
                border-bottom: 1px solid #ddd;
            }
            .table th {
                background-color: #f2f2f2;
            }
            .highlight {
                color: #e74c3c;
                font-weight: bold;
            }
            .significant {
                background-color: #d4edda;
            }
            .not-significant {
                background-color: #f8d7da;
            }
            .buttons {
                margin: 10px 0;
            }
            button {
                background-color: #4CAF50;
                color: white;
                padding: 8px 12px;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                margin-right: 5px;
            }
            button:hover {
                background-color: #45a049;
            }
            .phylotree-node circle {
                fill: #999;
            }
            .highlighted-node circle {
                fill: #e74c3c !important;
                r: 5 !important;
            }
            .highlighted-node text {
                fill: #e74c3c !important;
                font-weight: bold !important;
            }
            .highlighted-branch {
                stroke: #e74c3c !important;
                stroke-width: 3px !important;
            }
            .legend {
                margin-top: 10px;
                font-size: 0.9em;
            }
            .legend-item {
                display: inline-block;
                margin-right: 15px;
            }
            .legend-color {
                display: inline-block;
                width: 12px;
                height: 12px;
                margin-right: 5px;
                vertical-align: middle;
            }
            .download-links {
                margin-top: 20px;
            }
            .download-links a {
                display: inline-block;
                margin-right: 10px;
                padding: 5px 10px;
                background-color: #f8f9fa;
                border: 1px solid #ddd;
                border-radius: 3px;
                text-decoration: none;
                color: #333;
            }
            .download-links a:hover {
                background-color: #e9ecef;
            }
        </style>""")

            # JavaScript imports based on CDN preference
            use_cdn = getattr(self, 'js_cdn', True)
            if use_cdn:
                html_parts.append("""    <script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/phylotree@1.0.0-alpha.3/dist/phylotree.js"></script>""")
            else:
                html_parts.append("""    <!-- Embedded D3 and Phylotree libraries would go here -->
        <!-- This would make the file much larger -->""")

            html_parts.append("</head>")
            html_parts.append("<body>")

            # Body content
            html_parts.append(f"    <h1>MLDecay - Interactive Tree Visualization</h1>")
            html_parts.append(f"    <h2>Clade: {clade_id} - {taxa_display}</h2>")

            html_parts.append("    <div class=\"container\">")
            html_parts.append("        <div class=\"tree-section\">")
            html_parts.append("            <div class=\"buttons\">")
            html_parts.append("                <button onclick=\"tree.spacing_x(100).spacing_y(20).update()\">Expand Tree</button>")
            html_parts.append("                <button onclick=\"tree.spacing_x(30).spacing_y(10).update()\">Compact Tree</button>")
            html_parts.append("                <button onclick=\"resetTree()\">Reset View</button>")
            html_parts.append("                <button onclick=\"toggleLabels()\">Toggle Labels</button>")
            html_parts.append("            </div>")
            html_parts.append("            <div id=\"tree_container\"></div>")
            html_parts.append("            <div class=\"legend\">")
            html_parts.append("                <div class=\"legend-item\">")
            html_parts.append("                    <span class=\"legend-color\" style=\"background-color: #e74c3c;\"></span>")
            html_parts.append("                    <span>Highlighted Clade</span>")
            html_parts.append("                </div>")
            html_parts.append("                <div class=\"legend-item\">")
            html_parts.append("                    <span class=\"legend-color\" style=\"background-color: #999;\"></span>")
            html_parts.append("                    <span>Other Nodes</span>")
            html_parts.append("                </div>")
            html_parts.append("            </div>")
            html_parts.append("        </div>")

            # Info section
            html_parts.append("        <div class=\"info-section\">")
            html_parts.append("            <h3>Clade Information</h3>")
            html_parts.append("            <table class=\"table\">")
            html_parts.append(f"                <tr><th>Number of Taxa:</th><td>{len(highlight_taxa)} of {total_taxa} ({highlight_ratio:.1%})</td></tr>")

            taxa_sample = ", ".join(sorted(highlight_taxa)[:10])
            if len(highlight_taxa) > 10:
                taxa_sample += " ..."
            html_parts.append(f"                <tr><th>Taxa:</th><td>{taxa_sample}</td></tr>")

            # Bootstrap value if available
            if bootstrap_value is not None:
                html_parts.append(f"                <tr><th>Bootstrap Support:</th><td>{bootstrap_value}%</td></tr>")

            html_parts.append("            </table>")

            # Branch support section if available
            if au_data:
                html_parts.append("            <h3>Branch Support</h3>")
                html_parts.append("            <table class=\"table\">")

                significance_class = 'significant' if au_data.get('significant') else 'not-significant'
                significance_text = '(significant)' if au_data.get('significant') else '(not significant)'
                html_parts.append(f"                <tr><th>AU Test p-value:</th><td class=\"{significance_class}\">{au_data['au_pvalue']} {significance_text}</td></tr>")
                html_parts.append(f"                <tr><th>Log-Likelihood Difference:</th><td>{au_data['lnl_diff']}</td></tr>")

                html_parts.append("            </table>")

            # Site analysis section if available
            if site_data:
                html_parts.append("            <h3>Site Analysis</h3>")
                html_parts.append("            <table class=\"table\">")
                html_parts.append(f"                <tr><th>Supporting Sites:</th><td>{site_data['supporting']}</td></tr>")
                html_parts.append(f"                <tr><th>Conflicting Sites:</th><td>{site_data['conflicting']}</td></tr>")
                html_parts.append(f"                <tr><th>Support Ratio:</th><td>{site_data['support_ratio']}</td></tr>")
                html_parts.append(f"                <tr><th>Weighted Support Ratio:</th><td>{site_data['weighted_ratio']}</td></tr>")
                html_parts.append("            </table>")

            # Download section
            html_parts.append("            <h3>Downloads</h3>")
            html_parts.append("            <div class=\"download-links\">")
            html_parts.append(f"                <a href=\"{tree_path.name}\" download>Download Newick Tree</a>")
            html_parts.append("                <a href=\"#\" onclick=\"saveSvg()\">Download SVG</a>")
            html_parts.append("            </div>")
            html_parts.append("        </div>")
            html_parts.append("    </div>")

            # JavaScript section
            html_parts.append("    <script>")
            html_parts.append(f"        // Taxa to highlight")
            html_parts.append(f"        const highlightTaxa = {json.dumps(list(highlight_taxa))};")
            html_parts.append("")
            html_parts.append("        // Create tree")
            tree_path_js = str(tree_path.name).replace('\\', '/')
            html_parts.append(f"        let tree = new phylotree.phylotree(\"{tree_path_js}\");")
            html_parts.append("        let showLabels = true;")
            html_parts.append("")
            html_parts.append("        // Initialize visualization on page load")
            html_parts.append("        document.addEventListener(\"DOMContentLoaded\", function() {")
            html_parts.append("            loadAndDisplayTree();")
            html_parts.append("        });")
            html_parts.append("")
            html_parts.append("        function loadAndDisplayTree() {")
            html_parts.append(f"            fetch(\"{tree_path_js}\").then(response => {{")
            html_parts.append("                if (response.ok) {")
            html_parts.append("                    return response.text();")
            html_parts.append("                }")
            html_parts.append("                throw new Error('Tree file not found');")
            html_parts.append("            })")
            html_parts.append("            .then(treeData => {")
            html_parts.append("                // Set up tree visualization")
            html_parts.append("                tree = new phylotree.phylotree(treeData);")
            html_parts.append("")
            html_parts.append("                // Configure tree display settings")
            html_parts.append("                tree.branch_length(null)  // Use branch lengths from the tree")
            html_parts.append("                    .branch_name(function(node) {")
            html_parts.append("                        return node.data.name;")
            html_parts.append("                    })")
            html_parts.append("                    .node_span(function(node) {")
            html_parts.append("                        return showLabels ? 5 : 2;")
            html_parts.append("                    })")
            html_parts.append("                    .node_circle_size(function(node) {")
            html_parts.append("                        return isHighlighted(node) ? 5 : 3;")
            html_parts.append("                    })")
            html_parts.append("                    .font_size(14)")
            html_parts.append("                    .scale_bar_font_size(12)")
            html_parts.append("                    .node_styler(nodeStyler)")
            html_parts.append("                    .branch_styler(branchStyler)")
            html_parts.append("                    .layout_handler(phylotree.layout_handlers.radial)")
            html_parts.append("                    .spacing_x(40) // Controls horizontal spacing")
            html_parts.append("                    .spacing_y(15) // Controls vertical spacing")
            html_parts.append("                    .size([550, 550])")
            html_parts.append("                    .radial(false); // Start with rectangular layout")
            html_parts.append("")
            html_parts.append("                // Get the container")
            html_parts.append("                let container = document.getElementById('tree_container');")
            html_parts.append("")
            html_parts.append("                // Render the tree")
            html_parts.append("                tree.render(\"#tree_container\");")
            html_parts.append("            })")
            html_parts.append("            .catch(error => {")
            html_parts.append("                console.error(\"Error loading tree data:\", error);")
            html_parts.append("                document.getElementById('tree_container').innerHTML =")
            html_parts.append("                    \"<p style='color:red;padding:20px;'>Error loading tree data. Check console for details.</p>\";")
            html_parts.append("            });")
            html_parts.append("        }")
            html_parts.append("")
            html_parts.append("        // Style branches that belong to the highlighted clade")
            html_parts.append("        function branchStyler(dom_element, link_data) {")
            html_parts.append("            if (isHighlighted(link_data.target)) {")
            html_parts.append("                dom_element.style.stroke = \"#e74c3c\";")
            html_parts.append("                dom_element.style.strokeWidth = \"3px\";")
            html_parts.append("                dom_element.classList.add(\"highlighted-branch\");")
            html_parts.append("            }")
            html_parts.append("        }")
            html_parts.append("")
            html_parts.append("        // Style nodes that belong to the highlighted clade")
            html_parts.append("        function nodeStyler(dom_element, node_data) {")
            html_parts.append("            if (isHighlighted(node_data)) {")
            html_parts.append("                dom_element.classList.add(\"highlighted-node\");")
            html_parts.append("            }")
            html_parts.append("        }")
            html_parts.append("")
            html_parts.append("        // Check if a node belongs to the highlighted clade")
            html_parts.append("        function isHighlighted(node) {")
            html_parts.append("            if (!node.data) return false;")
            html_parts.append("")
            html_parts.append("            // Directly check leaf nodes")
            html_parts.append("            if (node.children && node.children.length === 0) {")
            html_parts.append("                return highlightTaxa.includes(node.data.name);")
            html_parts.append("            }")
            html_parts.append("")
            html_parts.append("            // For internal nodes, check if all descendants are in the highlighted taxa")
            html_parts.append("            let leaves = getAllLeaves(node);")
            html_parts.append("            let leafNames = leaves.map(leaf => leaf.data.name);")
            html_parts.append("")
            html_parts.append("            if (leafNames.length === 0) return false;")
            html_parts.append("")
            html_parts.append("            // Check if this node represents exactly our clade")
            html_parts.append("            // or is contained within our clade")
            html_parts.append("            return leafNames.every(name => highlightTaxa.includes(name));")
            html_parts.append("        }")
            html_parts.append("")
            html_parts.append("        // Get all leaves (terminal nodes) descending from a node")
            html_parts.append("        function getAllLeaves(node) {")
            html_parts.append("            if (!node.children || node.children.length === 0) {")
            html_parts.append("                return [node];")
            html_parts.append("            }")
            html_parts.append("")
            html_parts.append("            let leaves = [];")
            html_parts.append("            for (let child of node.children) {")
            html_parts.append("                leaves = leaves.concat(getAllLeaves(child));")
            html_parts.append("            }")
            html_parts.append("            return leaves;")
            html_parts.append("        }")
            html_parts.append("")
            html_parts.append("        // Reset tree to default view")
            html_parts.append("        function resetTree() {")
            html_parts.append("            tree.spacing_x(40)")
            html_parts.append("                .spacing_y(15)")
            html_parts.append("                .radial(false)")
            html_parts.append("                .update();")
            html_parts.append("        }")
            html_parts.append("")
            html_parts.append("        // Toggle display of leaf labels")
            html_parts.append("        function toggleLabels() {")
            html_parts.append("            showLabels = !showLabels;")
            html_parts.append("            tree.node_span(function(node) {")
            html_parts.append("                return showLabels ? 5 : 2;")
            html_parts.append("            }).update();")
            html_parts.append("        }")
            html_parts.append("")
            html_parts.append("        // Save tree as SVG")
            html_parts.append("        function saveSvg() {")
            html_parts.append("            let svg = document.querySelector(\"#tree_container svg\");")
            html_parts.append("            let serializer = new XMLSerializer();")
            html_parts.append("            let source = serializer.serializeToString(svg);")
            html_parts.append("")
            html_parts.append("            // Add name spaces")
            html_parts.append("            if (!source.match(/^<svg[^>]+xmlns=\"http:\\/\\/www\\.w3\\.org\\/2000\\/svg\"/)) {")
            html_parts.append("                source = source.replace(/^<svg/, '<svg xmlns=\"http://www.w3.org/2000/svg\"');")
            html_parts.append("            }")
            html_parts.append("            if (!source.match(/^<svg[^>]+\"http:\\/\\/www\\.w3\\.org\\/1999\\/xlink\"/)) {")
            html_parts.append("                source = source.replace(/^<svg/, '<svg xmlns:xlink=\"http://www.w3.org/1999/xlink\"');")
            html_parts.append("            }")
            html_parts.append("")
            html_parts.append("            // Add XML declaration")
            html_parts.append("            source = '<?xml version=\"1.0\" standalone=\"no\"?>\\r\\n' + source;")
            html_parts.append("")
            html_parts.append("            // Create download link")
            html_parts.append("            let downloadLink = document.createElement(\"a\");")
            html_parts.append("            downloadLink.href = \"data:image/svg+xml;charset=utf-8,\" + encodeURIComponent(source);")
            html_parts.append(f"            downloadLink.download = \"tree_{clade_id}.svg\";")
            html_parts.append("            document.body.appendChild(downloadLink);")
            html_parts.append("            downloadLink.click();")
            html_parts.append("            document.body.removeChild(downloadLink);")
            html_parts.append("        }")
            html_parts.append("    </script>")
            html_parts.append("</body>")
            html_parts.append("</html>")

            # Join all parts into the complete HTML
            html_content = "\n".join(html_parts)

            # Write the HTML file
            with open(html_path, 'w') as f:
                f.write(html_content)

            logger.info(f"Created interactive tree visualization for {clade_id}: {html_path}")
            return html_path

        except Exception as e:
            logger.error(f"Failed to create interactive tree visualization for {clade_id}: {e}")
            if self.debug:
                import traceback
                logger.debug(f"Traceback: {traceback.format_exc()}")
            return None

    def cleanup_intermediate_files(self):
        """
        Clean up intermediate files that are not needed for final output.
        This includes temporary .cleaned tree files and other intermediate files.
        """
        if self.debug or self.keep_files:
            logger.info("Skipping intermediate file cleanup due to debug or keep_files flag")
            return

        logger.info("Cleaning up intermediate files...")

        # Delete files explicitly marked for cleanup
        for file_path in self._files_to_cleanup:
            try:
                if file_path.exists():
                    file_path.unlink()
                    logger.debug(f"Deleted intermediate file: {file_path}")
            except Exception as e:
                logger.warning(f"Failed to delete intermediate file {file_path}: {e}")

        # Clean up other known intermediate files
        intermediate_patterns = [
            "*.cleaned",  # Cleaned tree files
            "constraint_tree_*.tre",  # Constraint trees
            "site_lnl_*.txt",  # Site likelihood files
            "ml_tree_for_*_annotation.nwk",  # Temporary annotation tree files
        ]

        for pattern in intermediate_patterns:
            for file_path in self.temp_path.glob(pattern):
                try:
                    file_path.unlink()
                    logger.debug(f"Deleted intermediate file: {file_path}")
                except Exception as e:
                    logger.warning(f"Failed to delete intermediate file {file_path}: {e}")


# --- Main Execution Logic ---
def print_runtime_parameters(args_ns, model_str_for_print):
    """Prints a summary of runtime parameters."""
    # (args_ns is the namespace from argparse.ArgumentParser)
    print("\n" + "=" * 80)
    print(f"MLDecay: ML-based Phylogenetic Decay Indices v{VERSION}")
    print("=" * 80)
    print("\nRUNTIME PARAMETERS:")
    print(f"  Alignment file:     {args_ns.alignment}") # Original string path is fine for print
    print(f"  Alignment format:   {args_ns.format}")
    print(f"  Data type:          {args_ns.data_type}")
    if args_ns.paup_block:
        print(f"  PAUP* settings:     User-provided block from '{args_ns.paup_block}'")
    else:
        print(f"  Model string:       {model_str_for_print}")
        # Further model details can be extracted from args_ns if needed
    print(f"\n  PAUP* executable:   {args_ns.paup}")
    print(f"  Threads for PAUP*:  {args_ns.threads}")
    if args_ns.starting_tree:
        print(f"  Starting tree:      {args_ns.starting_tree}")
    output_p = Path(args_ns.output) # Use Path for consistent name generation
    print("\nOUTPUT SETTINGS:")
    print(f"  Results file:       {output_p}")
    print(f"  Annotated trees:    {args_ns.tree}_au.nwk, {args_ns.tree}_lnl.nwk, {args_ns.tree}_combined.nwk")
    print(f"  Detailed report:    {output_p.with_suffix('.md')}")
    if args_ns.temp: print(f"  Temp directory:     {args_ns.temp}")
    if args_ns.debug: print(f"  Debug mode:         Enabled (log: mldecay_debug.log, if configured)")
    if args_ns.keep_files: print(f"  Keep temp files:    Enabled")
    if args_ns.visualize:
        print("\nVISUALIZATIONS:")
        print(f"  Enabled, format:    {args_ns.viz_format}")
        print(f"  Tree plot:          {output_p.parent / (output_p.stem + '_tree.' + args_ns.viz_format)}")
        if args_ns.html_trees:
            print(f"  HTML trees:         Enabled (using {'CDN' if args_ns.js_cdn else 'embedded'} JavaScript)")
        else:
            print(f"  HTML trees:         Disabled")
    print("\n" + "=" * 80 + "\n")

    @staticmethod
    def read_paup_block(paup_block_file_path: Path):
        if not paup_block_file_path.is_file():
            logger.error(f"PAUP block file not found: {paup_block_file_path}")
            return None
        try:
            content = paup_block_file_path.read_text()
            # Regex captures content *between* "BEGIN PAUP;" and "END;" (case-insensitive)
            match = re.search(r'BEGIN\s+PAUP\s*;(.*?)\s*END\s*;', content, re.DOTALL | re.IGNORECASE)
            if match:
                paup_cmds = match.group(1).strip()
                if not paup_cmds: logger.warning(f"PAUP block in {paup_block_file_path} is empty.")
                return paup_cmds
            else:
                logger.error(f"No valid PAUP block (BEGIN PAUP; ... END;) in {paup_block_file_path}")
                return None
        except Exception as e:
            logger.error(f"Error reading PAUP block file {paup_block_file_path}: {e}")
            return None


def main():
    parser = argparse.ArgumentParser(
        description=f"MLDecay v{VERSION}: Calculate ML-based phylogenetic decay indices using PAUP*.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter # Shows defaults in help
    )
    # Arguments (similar to original, ensure help messages are clear)
    parser.add_argument("alignment", help="Input alignment file path.")
    parser.add_argument("--format", default="fasta", help="Alignment format.")
    parser.add_argument("--model", default="GTR", help="Base substitution model (e.g., GTR, HKY, JC). Combine with --gamma and --invariable.")
    parser.add_argument("--gamma", action="store_true", help="Add Gamma rate heterogeneity (+G) to model.")
    parser.add_argument("--invariable", action="store_true", help="Add invariable sites (+I) to model.")

    parser.add_argument("--paup", default="paup", help="Path to PAUP* executable.")
    parser.add_argument("--output", default="ml_decay_indices.txt", help="Output file for summary results.")
    parser.add_argument("--tree", default="annotated_tree", help="Base name for annotated tree files. Three trees will be generated with suffixes: _au.nwk (AU p-values), _lnl.nwk (likelihood differences), and _combined.nwk (both values).")
    parser.add_argument("--site-analysis", action="store_true", help="Perform site-specific likelihood analysis to identify supporting/conflicting sites for each branch.")
    parser.add_argument("--data-type", default="dna", choices=["dna", "protein", "discrete"], help="Type of sequence data.")
    # Model parameter overrides
    mparams = parser.add_argument_group('Model Parameter Overrides (optional)')
    mparams.add_argument("--gamma-shape", type=float, help="Fixed Gamma shape value (default: estimate if +G).")
    mparams.add_argument("--prop-invar", type=float, help="Fixed proportion of invariable sites (default: estimate if +I).")
    mparams.add_argument("--base-freq", choices=["equal", "estimate", "empirical"], help="Base/state frequencies (default: model-dependent). 'empirical' uses observed frequencies.")
    mparams.add_argument("--rates", choices=["equal", "gamma"], help="Site rate variation model (overrides --gamma flag if specified).")
    mparams.add_argument("--protein-model", help="Specific protein model (e.g., JTT, WAG; overrides base --model for protein data).")
    mparams.add_argument("--nst", type=int, choices=[1, 2, 6], help="Number of substitution types (DNA; overrides model-based nst).")
    mparams.add_argument("--parsmodel", action=argparse.BooleanOptionalAction, default=None, help="Use parsimony-based branch lengths (discrete data; default: yes for discrete). Use --no-parsmodel to disable.")

    run_ctrl = parser.add_argument_group('Runtime Control')
    run_ctrl.add_argument("--threads", default="auto", help="Number of threads for PAUP* (e.g., 4 or 'auto').")
    run_ctrl.add_argument("--starting-tree", help="Path to a user-provided starting tree file (Newick).")
    run_ctrl.add_argument("--paup-block", help="Path to file with custom PAUP* commands for model/search setup (overrides most model args).")
    run_ctrl.add_argument("--temp", help="Custom directory for temporary files (default: system temp).")
    run_ctrl.add_argument("--keep-files", action="store_true", help="Keep temporary files after analysis.")
    run_ctrl.add_argument("--debug", action="store_true", help="Enable detailed debug logging (implies --keep-files).")

    # Add bootstrap options
    bootstrap_opts = parser.add_argument_group('Bootstrap Analysis (optional)')
    bootstrap_opts.add_argument("--bootstrap", action="store_true", help="Perform bootstrap analysis to calculate support values.")
    bootstrap_opts.add_argument("--bootstrap-reps", type=int, default=100, help="Number of bootstrap replicates (default: 100)")

    viz_opts = parser.add_argument_group('Visualization Output (optional)')
    viz_opts.add_argument("--visualize", action="store_true", help="Generate static visualization plots (requires matplotlib, seaborn).")
    viz_opts.add_argument("--viz-format", default="png", choices=["png", "pdf", "svg"], help="Format for static visualizations.")
    viz_opts.add_argument("--annotation", default="lnl", choices=["au", "lnl"], help="Type of support values to visualize in distribution plots (au=AU p-values, lnl=likelihood differences).")
    viz_opts.add_argument("--html-trees", action=argparse.BooleanOptionalAction, default=True, help="Generate interactive HTML tree visualizations (default: True)")
    viz_opts.add_argument("--js-cdn", action="store_true", default=True, help="Use CDN for JavaScript libraries (faster but requires internet connection)")
    viz_opts.add_argument("--keep-tree-files", action="store_true", default=False, help="Keep Newick tree files used for HTML visualization (default: False)")
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
        # Setup a dedicated debug file handler
        debug_log_path = Path.cwd() / "mldecay_debug.log" # Or in temp_path once it's known
        fh = logging.FileHandler(debug_log_path, mode='w') # Overwrite for each run
        fh.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)
        logger.info(f"Debug logging enabled. Detailed log: {debug_log_path}")
        args.keep_files = True # Debug implies keeping files

    # Construct full model string for display and internal use if not using paup_block
    effective_model_str = args.model
    if args.gamma: effective_model_str += "+G"
    if args.invariable: effective_model_str += "+I"
    # Adjust for protein/discrete if base model is not specific enough
    if args.data_type == "protein" and not args.protein_model and not any(pm in args.model.upper() for pm in ["JTT", "WAG", "LG", "DAYHOFF"]):
        logger.info(f"Protein data with generic model '{args.model}'. Effective model might default to JTT within PAUP* settings.")
        # effective_model_str might be JTT+G+I if G/I are on
    elif args.data_type == "discrete" and "MK" not in args.model.upper():
        logger.info(f"Discrete data with non-Mk model '{args.model}'. Effective model might default to Mk within PAUP* settings.")

    paup_block_content = None
    if args.paup_block:
        pbf_path = Path(args.paup_block)
        logger.info(f"Reading PAUP block from: {pbf_path}")
        paup_block_content = MLDecayIndices.read_paup_block(pbf_path)
        if paup_block_content is None: # Handles not found or invalid block
            logger.error("Failed to read or validate PAUP block file. Exiting.")
            sys.exit(1)

    print_runtime_parameters(args, effective_model_str)

    try:
        # Convert string paths from args to Path objects for MLDecayIndices
        temp_dir_path = Path(args.temp) if args.temp else None
        starting_tree_path = Path(args.starting_tree) if args.starting_tree else None

        decay_calc = MLDecayIndices(
            alignment_file=args.alignment, # Converted to Path in __init__
            alignment_format=args.format,
            model=effective_model_str, # Pass the constructed string
            temp_dir=temp_dir_path,
            paup_path=args.paup,
            threads=args.threads,
            starting_tree=starting_tree_path,
            data_type=args.data_type,
            debug=args.debug,
            keep_files=args.keep_files,
            gamma_shape=args.gamma_shape, prop_invar=args.prop_invar,
            base_freq=args.base_freq, rates=args.rates,
            protein_model=args.protein_model, nst=args.nst,
            parsmodel=args.parsmodel, # Pass the BooleanOptionalAction value
            paup_block=paup_block_content
        )

        decay_calc.build_ml_tree() # Can raise exceptions

        if decay_calc.ml_tree and decay_calc.ml_likelihood is not None:
            # Run bootstrap analysis if requested
            if args.bootstrap:
                logger.info(f"Running bootstrap analysis with {args.bootstrap_reps} replicates...")
                decay_calc.run_bootstrap_analysis(num_replicates=args.bootstrap_reps)

            decay_calc.calculate_decay_indices(perform_site_analysis=args.site_analysis)

            # Add this new code snippet here
            if hasattr(decay_calc, 'decay_indices') and decay_calc.decay_indices:
                for clade_id, data in decay_calc.decay_indices.items():
                    if 'site_data' in data:
                        site_output_dir = Path(args.output).parent / f"{Path(args.output).stem}_site_analysis"
                        decay_calc.write_site_analysis_results(site_output_dir)
                        logger.info(f"Site-specific analysis results written to {site_output_dir}")
                        break  # Only need to do this once if any site_data exists

            output_main_path = Path(args.output)
            decay_calc.write_results(output_main_path)

            report_path = output_main_path.with_suffix(".md")
            decay_calc.generate_detailed_report(report_path)

            output_dir = output_main_path.resolve().parent
            tree_base_name = args.tree  # Use the tree argument directly as the base name
            tree_files = decay_calc.annotate_trees(output_dir, tree_base_name)

            if tree_files:
                logger.info(f"Successfully created {len(tree_files)} annotated trees.")
                for tree_type, path in tree_files.items():
                    logger.info(f"  - {tree_type} tree: {path}")
            else:
                logger.warning("Failed to create annotated trees.")

            if args.visualize:
                viz_out_dir = output_main_path.resolve().parent # Ensure absolute path for parent
                viz_base_name = output_main_path.stem
                viz_kwargs = {'width': 10, 'height': 6, 'format': args.viz_format}

                # Check for viz library availability early
                try: import matplotlib, seaborn
                except ImportError:
                    logger.warning("Matplotlib/Seaborn not installed. Skipping static visualizations.")
                    args.visualize = False # Disable further attempts

                if args.visualize:
                    decay_calc.visualize_support_distribution(
                        viz_out_dir / f"{viz_base_name}_dist_{args.annotation}.{args.viz_format}",
                        value_type=args.annotation, **viz_kwargs)

                if args.site_analysis:
                    # Pass visualization preferences to the MLDecayIndices instance
                    if args.visualize:
                        decay_calc.generate_html = args.html_trees
                        decay_calc.js_cdn = args.js_cdn
                        decay_calc.viz_format = args.viz_format

                    site_output_dir = output_main_path.parent / f"{output_main_path.stem}_site_analysis"
                    decay_calc.write_site_analysis_results(site_output_dir, keep_tree_files=args.keep_tree_files)
                    logger.info(f"Site-specific analysis results written to {site_output_dir}")

            decay_calc.cleanup_intermediate_files()
            logger.info("MLDecay analysis completed successfully.")
        else:
            logger.error("ML tree construction failed or likelihood missing. Halting.")
            sys.exit(1) # Ensure exit if ML tree is critical and failed

    except Exception as e:
        logger.error(f"MLDecay analysis terminated with an error: {e}")
        if args.debug: # Print traceback in debug mode
            import traceback
            logger.debug("Full traceback:\n%s", traceback.format_exc())
        sys.exit(1)

    finally:
        # This block executes whether try succeeds or fails.
        # If decay_calc was initialized and keep_files is false, __del__ will handle cleanup.
        # If __init__ failed before self.temp_path was set, no specific cleanup here yet.
        if 'decay_calc' in locals() and (args.debug or args.keep_files):
            logger.info(f"Temporary files are preserved in: {decay_calc.temp_path}")


if __name__ == "__main__":
    main()
