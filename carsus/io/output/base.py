import logging
import numpy as np
import pandas as pd
import hashlib
import platform
import uuid
import pytz
from datetime import datetime
from carsus.util import (convert_wavelength_air2vacuum,
                         serialize_pandas_object,
                         hash_pandas_object)
from carsus.model import MEDIUM_VACUUM, MEDIUM_AIR
from astropy import units as u
from astropy import constants as const

# [nm], wavelengths above this value are given in air
# TODO: pass GFALL_AIR_THRESHOLD as parameter
GFALL_AIR_THRESHOLD = 200

P_EMISSION_DOWN = -1
P_INTERNAL_DOWN = 0
P_INTERNAL_UP = 1

logger = logging.getLogger(__name__)


class TARDISAtomData:

    """
    Attributes
    ----------
    levels_prepared : pandas.DataFrame
    lines_prepared : pandas.DataFrame
    macro_atom_prepared : pandas.DataFrame
    macro_atom_references_prepared : pandas.DataFrame

    Methods
    -------
    to_hdf(fname)
        Dump all attributes into an HDF5 file
    """

    def __init__(self,
                 atomic_weights,
                 ionization_energies,
                 gfall_reader,
                 zeta_data,
                 chianti_reader=None,
                 lines_loggf_threshold=-3,
                 levels_metastable_loggf_threshold=-3):

        # TODO: pass these params to the function as `gfall_params`
        self.levels_lines_param = {
            "levels_metastable_loggf_threshold":
            levels_metastable_loggf_threshold,
            "lines_loggf_threshold": lines_loggf_threshold
        }

        self.atomic_weights = atomic_weights
        self.ionization_energies = ionization_energies
        self.ground_levels = ionization_energies.get_ground_levels()

        # TODO: make this piece of code more readable 
        gfall_ions = gfall_reader.levels.index.tolist()
        # Remove last element from tuple (MultiIndex has 3 elements)
        gfall_ions = [x[:-1] for x in gfall_ions]
        # Keep unique tuples, list and sort them
        gfall_ions = sorted(list(set(gfall_ions)))
        self.gfall_reader = gfall_reader
        self.gfall_ions = gfall_ions

        self.zeta_data = zeta_data

        # TODO: priorities should not be managed by the `init` method.
        self.chianti_reader = chianti_reader
        if chianti_reader is not None:
            chianti_lvls = chianti_reader.levels.reset_index()
            chianti_lvls = chianti_lvls.set_index(
                ['atomic_number', 'ion_charge', 'priority'])

            mask = chianti_lvls.index.get_level_values(
                'priority') > self.gfall_reader.priority

            # TODO: make this piece of code more readable
            chianti_lvls = chianti_lvls[mask]
            chianti_ions = chianti_lvls.index.tolist()
            chianti_ions = [x[:-1] for x in chianti_ions]
            chianti_ions = sorted(list(set(chianti_ions)))
            self.chianti_ions = chianti_ions

        else:
            self.chianti_ions = []

        self.levels_all = self._get_all_levels_data().reset_index()
        self.lines_all = self._get_all_lines_data(self.levels_all)

        self._create_levels_lines(**self.levels_lines_param)
        self._create_macro_atom()
        self._create_macro_atom_references()

    @staticmethod
    def _create_artificial_fully_ionized(levels):
        """ Create artificial levels for fully ionized ions """
        fully_ionized_levels = list()

        for atomic_number, _ in levels.groupby("atomic_number"):
            fully_ionized_levels.append(
                (-1, atomic_number, atomic_number, 0, 0.0, 1, True)
            )

        levels_columns = ["level_id", "atomic_number",
                          "ion_number", "level_number",
                          "energy", "g", "metastable"]
        fully_ionized_levels_dtypes = [
            (key, levels.dtypes[key]) for key in levels_columns]

        fully_ionized_levels = np.array(
            fully_ionized_levels, dtype=fully_ionized_levels_dtypes)

        return pd.DataFrame(data=fully_ionized_levels)

    @staticmethod
    def _create_metastable_flags(levels, lines,
                                 levels_metastable_loggf_threshold=-3):
        # Filter lines on the loggf threshold value
        metastable_lines = lines.loc[lines["loggf"]
                                     > levels_metastable_loggf_threshold]

        # Count the remaining strong transitions
        metastable_lines_grouped = metastable_lines.groupby("upper_level_id")
        metastable_counts = metastable_lines_grouped["upper_level_id"].count()
        metastable_counts.name = "metastable_counts"

        # If there are no strong transitions for a level (the count is NaN)
        # then the metastable flag is True else (the count is a natural number)
        # the metastable flag is False
        levels = levels.join(metastable_counts)
        metastable_flags = levels["metastable_counts"].isnull()
        metastable_flags.name = "metastable"

        return metastable_flags

    @staticmethod
    def _create_einstein_coeff(lines):
        einstein_coeff = (4 * np.pi ** 2 * const.e.gauss.value **
                          2) / (const.m_e.cgs.value * const.c.cgs.value)
        lines['B_lu'] = einstein_coeff * lines['f_lu'] / \
            (const.h.cgs.value * lines['nu'])
        lines['B_ul'] = einstein_coeff * lines['f_ul'] / \
            (const.h.cgs.value * lines['nu'])
        lines['A_ul'] = 2 * einstein_coeff * lines['nu'] ** 2 / \
            const.c.cgs.value ** 2 * lines['f_ul']

    def _get_all_levels_data(self):
        """ Returns the same output than `AtomData._get_all_levels_data()` """
        logger.info('Ingesting levels from GFALL.')
        gf_levels = self.gfall_reader.levels.reset_index()
        gf_levels['source'] = 'gfall'

        if len(self.chianti_ions) > 0:
            logger.info('Ingesting levels from Chianti.')
            ch_levels = self.chianti_reader.levels.reset_index()
            ch_levels['source'] = 'chianti'
        else:
            ch_levels = pd.DataFrame(columns=gf_levels.columns)

        levels = pd.concat([gf_levels, ch_levels], sort=True)
        levels['g'] = 2*levels['j'] + 1
        levels['g'] = levels['g'].astype(np.int)
        levels = levels.drop(columns=['j', 'label', 'method'])
        levels = levels.reset_index(drop=True)
        levels = levels.rename(columns={'ion_charge': 'ion_number'})
        levels = levels[['atomic_number',
                         'ion_number', 'g', 'energy', 'source']]

        levels['energy'] = levels['energy'].apply(lambda x: x*u.Unit('cm-1'))
        levels['energy'] = levels['energy'].apply(
            lambda x: x.to(u.eV, equivalencies=u.spectral()))
        levels['energy'] = levels['energy'].apply(lambda x: x.value)

        ground_levels = self.ground_levels
        ground_levels.rename(
            columns={'ion_charge': 'ion_number'}, inplace=True)
        ground_levels['source'] = 'nist'

        levels = pd.concat([ground_levels, levels], sort=True)
        levels['level_id'] = range(1, len(levels)+1)
        levels = levels.set_index('level_id')

        # Deliberately keep the "duplicated" Chianti levels.
        # These levels are not strictly duplicated: same energy
        # for different configurations. 
        # 
        # e.g. ChiantiIonReader('h_1')
        #
        # In fact, the following code should only remove the du-
        # plicated ground levels. Other duplicated levels should
        # be removed at the reader stage.
        #
        # TODO: a more clear way to get the same result could be: 
        # "keep only zero energy levels from NIST source".

        mask = (levels['source'] != 'chianti') & (
            levels[['atomic_number', 'ion_number',
                    'energy', 'g']].duplicated(keep='last'))	
        levels = levels[~mask]

        # Keep higher priority levels over GFALL: if levels with
        # different source than 'gfall' made to this point should
        # be kept.
        for ion in self.chianti_ions:
            mask = (levels['source'] == 'gfall') & (
                levels['atomic_number'] == ion[0]) & (
                    levels['ion_number'] == ion[1])
            levels.drop(levels[mask].index, inplace=True)

        levels = levels[['atomic_number',
                         'ion_number', 'g', 'energy', 'source']]

        return levels

    def _get_all_lines_data(self, levels):
        """ Returns the same output than `AtomData._get_all_lines_data()` """
        gf = self.gfall_reader
        ch = self.chianti_reader

        start = 1
        gf_list = []
        logger.info('Ingesting lines from GFALL.')
        for ion in self.gfall_ions:

            try:
                df = gf.lines.loc[ion]

                # To match `line_id` field with the old API we keep
                # track of how many GFALL lines we are skipping.
                if ion in self.chianti_ions:
                    df['line_id'] = range(start, len(df) + start)
                    start += len(df)
                    continue

                else:
                    df['line_id'] = range(start, len(df) + start)
                    start += len(df)

            except (KeyError, TypeError):
                continue

            df['source'] = 'gfall'

            # TODO: move this piece of code to a staticmethod
            df = df.reset_index()
            lvl_index2id = levels.set_index(
                ['atomic_number', 'ion_number']).loc[ion]
            lvl_index2id = lvl_index2id.reset_index()
            lvl_index2id = lvl_index2id[['level_id']]

            lower_level_id = []
            upper_level_id = []
            for i, row in df.iterrows():

                llid = int(row['level_index_lower'])
                ulid = int(row['level_index_upper'])

                upper = int(lvl_index2id.loc[ulid])
                lower = int(lvl_index2id.loc[llid])

                lower_level_id.append(lower)
                upper_level_id.append(upper)

            df['lower_level_id'] = pd.Series(lower_level_id)
            df['upper_level_id'] = pd.Series(upper_level_id)
            gf_list.append(df)

        ch_list = []
        logger.info('Ingesting lines from Chianti.')
        for ion in self.chianti_ions:

            df = ch.lines.loc[ion]
            df['line_id'] = range(start, len(df) + start)
            start = len(df) + start
            df['source'] = 'chianti'

            # TODO: move this piece of code to a staticmethod
            df = df.reset_index()
            lvl_index2id = levels.set_index(
                ['atomic_number', 'ion_number']).loc[ion]
            lvl_index2id = lvl_index2id.reset_index()
            lvl_index2id = lvl_index2id[['level_id']]

            lower_level_id = []
            upper_level_id = []
            for i, row in df.iterrows():

                llid = int(row['level_index_lower'])
                ulid = int(row['level_index_upper'])

                upper = int(lvl_index2id.loc[ulid])
                lower = int(lvl_index2id.loc[llid])

                lower_level_id.append(lower)
                upper_level_id.append(upper)

            df['lower_level_id'] = pd.Series(lower_level_id)
            df['upper_level_id'] = pd.Series(upper_level_id)
            ch_list.append(df)

        df_list = gf_list + ch_list
        lines = pd.concat(df_list, sort=True)
        lines['loggf'] = lines['gf'].apply(np.log10)

        lines.set_index('line_id', inplace=True)
        lines.drop(columns=['energy_upper', 'j_upper', 'energy_lower',
                            'j_lower', 'level_index_lower',
                            'level_index_upper'], inplace=True)

        lines.loc[lines['wavelength'] <=
                  GFALL_AIR_THRESHOLD, 'medium'] = MEDIUM_VACUUM
        lines.loc[lines['wavelength'] >
                  GFALL_AIR_THRESHOLD, 'medium'] = MEDIUM_AIR

        air_mask = lines['medium'] == MEDIUM_AIR
        gfall_mask = lines['source'] == 'gfall'
        chianti_mask = lines['source'] == 'chianti'

        lines.loc[gfall_mask, 'wavelength'] = lines.loc[
            gfall_mask, 'wavelength'].apply(lambda x: x*u.nm)
        lines.loc[chianti_mask, 'wavelength'] = lines.loc[
            chianti_mask, 'wavelength'].apply(lambda x: x*u.angstrom)
        lines['wavelength'] = lines['wavelength'].apply(
            lambda x: x.to('angstrom'))
        lines['wavelength'] = lines['wavelength'].apply(lambda x: x.value)

        # Why not for Chianti?
        lines.loc[air_mask & gfall_mask,
                  'wavelength'] = convert_wavelength_air2vacuum(
            lines.loc[air_mask, 'wavelength'])

        lines.drop(columns=['medium'], inplace=True)
        lines = lines[['lower_level_id', 'upper_level_id',
                       'wavelength', 'gf', 'loggf', 'source']]

        return lines

    def _create_levels_lines(self, lines_loggf_threshold=-3,
                             levels_metastable_loggf_threshold=-3):
        """ Returns almost the same output than
        `AtomData.create_levels_lines` method """
        levels_all = self.levels_all
        lines_all = self.lines_all
        ionization_energies = self.ionization_energies.base.reset_index()
        ionization_energies['ion_number'] -= 1

        # Culling autoionization levels
        levels_w_ionization_energies = pd.merge(levels_all,
                                                ionization_energies,
                                                how='left',
                                                on=["atomic_number",
                                                    "ion_number"])
        mask = levels_w_ionization_energies["energy"] < \
            levels_w_ionization_energies["ionization_energy"]
        levels = levels_w_ionization_energies[mask].copy()
        levels = levels.set_index('level_id').sort_values(
            by=['atomic_number', 'ion_number'])
        levels = levels.drop(columns='ionization_energy')

        # Clean lines
        lines = lines_all.join(pd.DataFrame(index=levels.index),
                               on="lower_level_id", how="inner").\
            join(pd.DataFrame(index=levels.index),
                 on="upper_level_id", how="inner")

        # Culling lines with low gf values
        lines = lines.loc[lines["loggf"] > lines_loggf_threshold]

        # Do not clean levels that don't exist in lines

        # Create the metastable flags for levels
        levels["metastable"] = \
            self._create_metastable_flags(levels, lines_all,
                                          levels_metastable_loggf_threshold)

        # Create levels numbers
        levels = levels.sort_values(
            ["atomic_number", "ion_number", "energy", "g"])
        levels["level_number"] = levels.groupby(['atomic_number',
                                                 'ion_number'])['energy']. \
            transform(lambda x: np.arange(len(x))).values
        levels["level_number"] = levels["level_number"].astype(np.int)

        levels = levels[['atomic_number', 'energy', 'g', 'ion_number',
                         'level_number', 'metastable']]

        # Join atomic_number, ion_number, level_number_lower,
        # level_number_upper on lines
        lower_levels = levels.rename(
            columns={
                "level_number": "level_number_lower",
                "g": "g_l"}
        ).loc[:, ["atomic_number", "ion_number", "level_number_lower", "g_l"]]
        upper_levels = levels.rename(
            columns={
                "level_number": "level_number_upper",
                "g": "g_u"}
        ).loc[:, ["level_number_upper", "g_u"]]
        lines = lines.join(lower_levels, on="lower_level_id").join(
            upper_levels, on="upper_level_id")

        # Calculate absorption oscillator strength f_lu and emission
        # oscillator strength f_ul
        lines["f_lu"] = lines["gf"] / lines["g_l"]
        lines["f_ul"] = lines["gf"] / lines["g_u"]

        # Calculate frequency
        lines['nu'] = u.Quantity(
            lines['wavelength'], 'angstrom').to('Hz', u.spectral())

        # Create Einstein coefficients
        self._create_einstein_coeff(lines)

        # Reset indexes because `level_id` cannot be an index once we
        # add artificial levels for fully ionized ions that don't have ids (-1)
        lines = lines.reset_index()
        levels = levels.reset_index()

        # Create and append artificial levels for fully ionized ions
        artificial_fully_ionized_levels = \
            self._create_artificial_fully_ionized(levels)
        levels = levels.append(
            artificial_fully_ionized_levels, ignore_index=True)
        levels = levels.sort_values(
            ["atomic_number", "ion_number", "level_number"])

        self.lines = lines
        self.levels = levels

    @property
    def levels_prepared(self):
        """
        Prepare the DataFrame with levels for TARDIS
        Returns
        -------
        levels_prepared: pandas.DataFrame
            DataFrame with:
                index: none;
                columns: atomic_number, ion_number, level_number,
                            energy[eV], g[1], metastable.
        """

        levels_prepared = self.levels.loc[:, [
            "atomic_number", "ion_number", "level_number",
            "energy", "g", "metastable"]].copy()

        # Set index
        levels_prepared.set_index(
            ["atomic_number", "ion_number", "level_number"], inplace=True)

        return levels_prepared

    @property
    def lines_prepared(self):
        """
            Prepare the DataFrame with lines for TARDIS
            Returns
            -------
            lines_prepared : pandas.DataFrame
                DataFrame with:
                    index: none;
                    columns: line_id, atomic_number, ion_number,
                             level_number_lower, level_number_upper,
                             wavelength[angstrom], nu[Hz], f_lu[1], f_ul[1],
                             B_ul[cm^3 s^-2 erg^-1], B_lu[cm^3 s^-2 erg^-1],
                             A_ul[1/s].
        """

        lines_prepared = self.lines.loc[:, [
            "line_id", "wavelength", "atomic_number", "ion_number",
            "f_ul", "f_lu", "level_number_lower", "level_number_upper",
            "nu", "B_lu", "B_ul", "A_ul"]].copy()

        # Set the index
        lines_prepared.set_index([
            "atomic_number", "ion_number",
            "level_number_lower", "level_number_upper"], inplace=True)

        return lines_prepared

    def _create_macro_atom(self):
        """
            Create a DataFrame containing *macro atom* data.
            Returns
            -------
            macro_atom: pandas.DataFrame
                DataFrame with:
                    index: none;
                    columns: atomic_number, ion_number, source_level_number,
                             target_level_number, transition_line_id,
                             transition_type, transition_probability.
            Notes:
                Refer to the docs:
                https://tardis-sn.github.io/tardis/physics/plasma/macroatom.html
        """
        # Exclude artificially created levels from levels
        levels = self.levels.loc[self.levels["level_id"]
                                 != -1].set_index("level_id")

        lvl_energy_lower = levels.rename(
            columns={"energy": "energy_lower"}).loc[:, ["energy_lower"]]
        lvl_energy_upper = levels.rename(
            columns={"energy": "energy_upper"}).loc[:, ["energy_upper"]]

        lines = self.lines.set_index("line_id")
        lines = lines.join(lvl_energy_lower, on="lower_level_id").join(
            lvl_energy_upper, on="upper_level_id")

        macro_atom = list()
        macro_atom_dtype = [("atomic_number", np.int), ("ion_number", np.int),
                            ("source_level_number",
                             np.int), ("target_level_number", np.int),
                            ("transition_line_id",
                             np.int), ("transition_type", np.int),
                            ("transition_probability", np.float)]

        for line_id, row in lines.iterrows():
            atomic_number, ion_number = row["atomic_number"], row["ion_number"]
            level_number_lower, level_number_upper = \
                row["level_number_lower"], row["level_number_upper"]
            nu = row["nu"]
            f_ul, f_lu = row["f_ul"], row["f_lu"]
            e_lower, e_upper = row["energy_lower"], row["energy_upper"]

            transition_probabilities_dict = dict()  # type : probability
            transition_probabilities_dict[P_EMISSION_DOWN] = 2 * \
                nu**2 * f_ul / const.c.cgs.value**2 * (e_upper - e_lower)
            transition_probabilities_dict[P_INTERNAL_DOWN] = 2 * \
                nu**2 * f_ul / const.c.cgs.value**2 * e_lower
            transition_probabilities_dict[P_INTERNAL_UP] = f_lu * \
                e_lower / (const.h.cgs.value * nu)

            macro_atom.append((atomic_number, ion_number, level_number_upper,
                               level_number_lower, line_id, P_EMISSION_DOWN,
                               transition_probabilities_dict[P_EMISSION_DOWN]))
            macro_atom.append((atomic_number, ion_number, level_number_upper,
                               level_number_lower, line_id, P_INTERNAL_DOWN,
                               transition_probabilities_dict[P_INTERNAL_DOWN]))
            macro_atom.append((atomic_number, ion_number, level_number_lower,
                               level_number_upper, line_id, P_INTERNAL_UP,
                               transition_probabilities_dict[P_INTERNAL_UP]))

        macro_atom = np.array(macro_atom, dtype=macro_atom_dtype)
        macro_atom = pd.DataFrame(macro_atom)

        macro_atom = macro_atom.sort_values(
            ["atomic_number", "ion_number", "source_level_number"])

        self.macro_atom = macro_atom

    @property
    def macro_atom_prepared(self):
        """
            Prepare the DataFrame with macro atom data for TARDIS
            Returns
            -------
            macro_atom_prepared : pandas.DataFrame
                DataFrame with the *macro atom data* with:
                    index: none;
                    columns: atomic_number, ion_number, source_level_number,
                             destination_level_number, transition_line_id
                             transition_type, transition_probability.
            Notes:
                Refer to the docs:
                https://tardis-sn.github.io/tardis/physics/plasma/macroatom.html
        """

        macro_atom_prepared = self.macro_atom.loc[:, [
            "atomic_number",
            "ion_number", "source_level_number", "target_level_number",
            "transition_type", "transition_probability",
            "transition_line_id"]].copy()

        # ToDo: choose between `target_level_number` and
        # `destination_level_number` Rename `target_level_number` to
        # `destination_level_number` used in TARDIS Personally, I think
        # `target_level_number` is better so I use it in Carsus.
        macro_atom_prepared = macro_atom_prepared.rename(columns={
            "target_level_number": "destination_level_number"})

        macro_atom_prepared = macro_atom_prepared.reset_index(drop=True)

        return macro_atom_prepared

    def _create_macro_atom_references(self):
        """
            Create a DataFrame containing *macro atom reference* data.
            Returns
            -------
            macro_atom_reference : pandas.DataFrame
                DataFrame with:
                index: no index;
                and columns: atomic_number, ion_number, source_level_number,
                count_down, count_up, count_total
        """
        macro_atom_references = self.levels.rename(
            columns={"level_number": "source_level_number"}).\
            loc[:, ["atomic_number", "ion_number",
                    "source_level_number", "level_id"]]

        count_down = self.lines.groupby("upper_level_id").size()
        count_down.name = "count_down"

        count_up = self.lines.groupby("lower_level_id").size()
        count_up.name = "count_up"

        macro_atom_references = macro_atom_references.join(
            count_down, on="level_id").join(count_up, on="level_id")
        macro_atom_references = macro_atom_references.drop("level_id", axis=1)

        macro_atom_references = macro_atom_references.fillna(0)
        macro_atom_references["count_total"] = 2 * \
            macro_atom_references["count_down"] + \
            macro_atom_references["count_up"]

        # Convert to int
        macro_atom_references["count_down"] = \
            macro_atom_references["count_down"].astype(np.int)
        macro_atom_references["count_up"] = \
            macro_atom_references["count_up"].astype(np.int)
        macro_atom_references["count_total"] = \
            macro_atom_references["count_total"].astype(np.int)

        self.macro_atom_references = macro_atom_references

    @property
    def macro_atom_references_prepared(self):
        """
            Prepare the DataFrame with macro atom references for TARDIS
            Returns
            -------
            macro_atom_references_prepared : pandas.DataFrame
                DataFrame with:
                    index: none;
                    columns: atomic_number, ion_number, source_level_number,
                             count_down, count_up, count_total.
        """
        macro_atom_references_prepared = self.macro_atom_references.loc[:, [
            "atomic_number", "ion_number", "source_level_number", "count_down",
            "count_up", "count_total"]].copy()

        macro_atom_references_prepared.set_index(
            ['atomic_number', 'ion_number', 'source_level_number'],
            inplace=True)

        return macro_atom_references_prepared

    def to_hdf(self, fname):
        """Dump the `base` attribute into an HDF5 file
        Parameters
        ----------
        fname : path
           Path to the HDF5 output file
        """

        with pd.HDFStore(fname, 'w') as f:
            f.put('/atom_data', self.atomic_weights.base)
            f.put('/ionization_data', self.ionization_energies.base)
            f.put('/zeta_data', self.zeta_data.base)
            f.put('/levels', self.levels_prepared)
            f.put('/lines', self.lines_prepared)
            f.put('/macro_atom_data', self.macro_atom_prepared)
            f.put('/macro_atom_references',
                  self.macro_atom_references_prepared)

            meta = []
            md5_hash = hashlib.md5()
            for key in f.keys():
                # Update the total MD5 sum
                md5_hash.update(serialize_pandas_object(f[key]).to_buffer())
                
                # Save the individual Series/DataFrame MD5
                md5 = hash_pandas_object(f[key])
                meta.append(('md5sum', key.lstrip('/'), md5[:20]))

            # Save datasets versions
            meta.append(('datasets', 'nist_weights', 
                         self.atomic_weights.version))
            meta.append(('datasets', 'nist_spectra', 
                         self.ionization_energies.version))

            meta.append(('datasets', 'gfall.dat',
                         self.gfall_reader.md5[:20]))

            if self.chianti_reader is not None:
                meta.append(('datasets', 'chianti_data', 
                             self.chianti_reader.version))

            # Save relevant package versions
            meta.append(('software', 'python', platform.python_version()))
            imports = ['carsus', 'astropy', 'numpy', 'pandas', 'pyarrow', 
                       'tables', 'ChiantiPy']
 
            for package in imports:
                meta.append(('software', package,
                             __import__(package).__version__))

            meta_df = pd.DataFrame.from_records(meta, columns=['field', 'key',
                        'value'], index=['field', 'key'])

            uuid1 = uuid.uuid1().hex

            logger.info(f"Signing TARDISAtomData.")
            logger.info(f"MD5: {md5_hash.hexdigest()}")
            logger.info(f"UUID1: {uuid1}")

            f.root._v_attrs['md5'] = md5_hash.hexdigest()
            f.root._v_attrs['uuid1'] = uuid1
            f.put('/meta', meta_df)

            utc = pytz.timezone('UTC')
            timestamp = datetime.now(utc).strftime("%b %d, %Y %H:%M:%S UTC")
            f.root._v_attrs['date'] = timestamp

            self.meta = meta_df
            
            return