"""Resolve chemical names in paper text and draw them with RDKit.

The resolver is local on purpose. These extracts are full of abbreviations
(PCy3, IMes) and hyphenated IUPAC (2-chloro-5-methoxypyridine), which a
name-to-structure lookup misses or mis-assigns. Three passes cover them:

1. A dictionary of names and abbreviations whose structure is unambiguous.
2. A phosphine formula reader (PPh3, PCy2Ph, P(t-Bu)3, P(OMe)3).
3. A locant–substituent–parent grammar for substituted benzenes, pyridines,
   phosphines, aziridines, and benzenesulfonamides (2,4-dichloropyridine,
   2-benzyl-1-tosylaziridine).

Longest match wins, so "2-chloropyridine" is kept and "pyridine" inside it
is not drawn again.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field

from rdkit import Chem
from rdkit.Chem import rdChemReactions
from rdkit.Chem.Draw import rdMolDraw2D


# Substituent SMILES. The dummy atom is the attachment point.
GROUPS: dict[str, str] = {
    "chloro": "*Cl",
    "bromo": "*Br",
    "fluoro": "*F",
    "iodo": "*I",
    "methyl": "*C",
    "ethyl": "*CC",
    "propyl": "*CCC",
    "isopropyl": "*C(C)C",
    "butyl": "*CCCC",
    "secbutyl": "*C(C)CC",
    "tertbutyl": "*C(C)(C)C",
    "phenyl": "*c1ccccc1",
    "methoxy": "*OC",
    "ethoxy": "*OCC",
    "isopropoxy": "*OC(C)C",
    "phenoxy": "*Oc1ccccc1",
    "nitro": "*[N+](=O)[O-]",
    "amino": "*N",
    "hydroxy": "*O",
    "cyano": "*C#N",
    "trifluoromethyl": "*C(F)(F)F",
    "trifluoromethoxy": "*OC(F)(F)F",
    "morpholino": "*N1CCOCC1",
    "benzyl": "*Cc1ccccc1",
    "methylcarboxamide": "*C(=O)NC",
    "n-methylcarboxamide": "*C(=O)NC",
    "dimethylamino": "*N(C)C",
    "cyclohexyl": "*C1CCCCC1",
    "cyclopentyl": "*C1CCCC1",
    "vinyl": "*C=C",
    "acetyl": "*C(C)=O",
    "formyl": "*C=O",
    "tosyl": "*S(=O)(=O)c1ccc(C)cc1",
}

# parent key -> (smiles, locant -> atom index, monosubstitution is unambiguous)
PARENTS: dict[str, tuple[str, dict[int, int], bool]] = {
    "benzene": ("c1ccccc1", {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}, True),
    "pyridine": ("n1ccccc1", {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}, False),
    "pyrimidine": ("n1ccncc1", {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}, False),
    "quinoline": (
        "n1cccc2ccccc12",
        {1: 0, 2: 1, 3: 2, 4: 3, 5: 5, 6: 6, 7: 7, 8: 8},
        False,
    ),
    "naphthalene": (
        "c1ccc2ccccc2c1",
        {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 7},
        False,
    ),
    # Locant 1 is the ipso carbon, not the heteroatom in front of the ring.
    "aniline": ("Nc1ccccc1", {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}, False),
    "phenol": ("Oc1ccccc1", {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}, False),
    "benzaldehyde": ("O=Cc1ccccc1", {1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 7}, False),
    "benzonitrile": ("N#Cc1ccccc1", {1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 7}, False),
    # N is locant 1. The two carbons are locants 2 and 3.
    "aziridine": ("N1CC1", {1: 0, 2: 1, 3: 2}, False),
    # Locant 1 is the carbon that bears the sulfonamide. Locant 4 is opposite it.
    "benzenesulfonamide": ("c1ccccc1S(=O)(=O)N", {1: 5, 2: 0, 3: 1, 4: 2, 5: 3, 6: 4}, False),
}

MULTIPLIERS = (
    ("penta", 5),
    ("tetra", 4),
    ("hexa", 6),
    ("tris", 3),
    ("bis", 2),
    ("tri", 3),
    ("di", 2),
)

# Longer keys first so "trifluoromethyl" beats "methyl" and "tri".
_GROUP_KEYS = tuple(sorted(GROUPS, key=len, reverse=True))

_SUBST_WORD = (
    "chloro|bromo|fluoro|iodo|methyl|methoxy|ethoxy|nitro|phenyl|amino|"
    "hydroxy|cyano|trifluoromethyl|trifluoromethoxy|morpholino|dimethylamino"
)


@dataclass
class Structure:
    smiles: str
    title: str
    kind: str  # molecule, ligand, group, solvent
    note: str = ""
    mid: str = ""


@dataclass
class Span:
    start: int
    end: int
    surface: str
    structure: Structure
    priority: int  # lower wins when lengths tie: lexicon 0, formula 1, grammar 2


@dataclass
class Scheme:
    title: str
    caption: str
    mode: str  # reaction | comparison
    reaction: str = ""
    molecules: list[tuple[str, str]] = field(default_factory=list)


def _mol(smiles: str) -> Chem.Mol | None:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    try:
        Chem.SanitizeMol(mol)
    except Chem.MolSanitizeException:
        return None
    return mol


def attach_group(mol: Chem.Mol, atom_idx: int, group_smiles: str) -> Chem.Mol | None:
    """Single-bond a substituent onto atom_idx. group_smiles uses * as the attachment."""
    group = Chem.MolFromSmiles(group_smiles)
    if group is None:
        return None
    dummy = next((atom.GetIdx() for atom in group.GetAtoms() if atom.GetAtomicNum() == 0), None)
    if dummy is None:
        return None
    neighbors = group.GetAtomWithIdx(dummy).GetNeighbors()
    if len(neighbors) != 1:
        return None
    neighbor_idx = neighbors[0].GetIdx()
    bond_type = group.GetBondBetweenAtoms(dummy, neighbor_idx).GetBondType()
    editable = Chem.RWMol(group)
    editable.RemoveAtom(dummy)
    if dummy < neighbor_idx:
        neighbor_idx -= 1
    combined = Chem.CombineMols(mol, editable.GetMol())
    out = Chem.EditableMol(combined)
    out.AddBond(atom_idx, mol.GetNumAtoms() + neighbor_idx, bond_type)
    result = out.GetMol()
    try:
        Chem.SanitizeMol(result)
    except Chem.MolSanitizeException:
        return None
    return result


def _build_ring(parent: str, substitutions: list[tuple[int, str]]) -> Chem.Mol | None:
    smiles, locants, _ = PARENTS[parent]
    mol = _mol(smiles)
    if mol is None:
        return None
    for locant, key in substitutions:
        mol = attach_group(mol, locants[locant], GROUPS[key])
        if mol is None:
            return None
    return mol


def _split_prefix(prefix: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for char in prefix:
        if char == "(":
            depth += 1
            buf.append(char)
        elif char == ")":
            depth = max(0, depth - 1)
            buf.append(char)
        elif char == "-" and depth == 0:
            if buf:
                parts.append("".join(buf))
                buf = []
        else:
            buf.append(char)
    if buf:
        parts.append("".join(buf))
    return [part for part in parts if part]


def _locants(token: str) -> list[int] | None:
    if re.fullmatch(r"\d+(?:,\d+)*", token):
        return [int(piece) for piece in token.split(",")]
    return None


def _ensure_yl(token: str) -> str | None:
    """Turn 'naphthalen-2-yl' into a substituent stored on GROUPS."""
    match = re.fullmatch(r"([a-z]+)-(\d+)-yl", token)
    if match is None:
        return None
    stem, locant = match.group(1), int(match.group(2))
    parent = stem + "e" if stem + "e" in PARENTS else stem if stem in PARENTS else None
    if parent is None:
        return None
    _smiles, locants, _mono = PARENTS[parent]
    if locant not in locants or token in GROUPS:
        return token if token in GROUPS else None
    mol = _mol(_smiles)
    if mol is None:
        return None
    editable = Chem.RWMol(mol)
    dummy = editable.AddAtom(Chem.Atom(0))
    editable.AddBond(locants[locant], dummy, Chem.BondType.SINGLE)
    try:
        Chem.SanitizeMol(editable.GetMol())
    except Chem.MolSanitizeException:
        return None
    group_smiles = Chem.MolToSmiles(editable.GetMol())
    if "*" not in group_smiles:
        return None
    GROUPS[token] = group_smiles
    return token


def _subst_keys(token: str) -> list[str] | None:
    token = token.strip("()").lower().replace(" ", "")
    if token in GROUPS:
        return [token]
    yl = _ensure_yl(token)
    if yl:
        return [yl]
    aliases = {"cl": "chloro", "br": "bromo", "nmethylcarboxamide": "n-methylcarboxamide"}
    if token in aliases:
        return [aliases[token]]
    for prefix, count in MULTIPLIERS:
        if token.startswith(prefix):
            rest = token[len(prefix):].strip("()")
            if rest in GROUPS:
                return [rest] * count
    return None


def _parse_substitutions(prefix: str) -> list[tuple[int | None, str]] | None:
    if prefix == "":
        return []
    parts = _split_prefix(prefix)
    found: list[tuple[int | None, str]] = []
    index = 0
    while index < len(parts):
        locs = _locants(parts[index])
        if locs is not None and index + 1 < len(parts):
            keys = _subst_keys(parts[index + 1])
            if keys is None or len(keys) != len(locs):
                return None
            for locant, key in zip(locs, keys):
                found.append((locant, key))
            index += 2
            continue
        keys = _subst_keys(parts[index])
        if keys is None:
            return None
        for key in keys:
            found.append((None, key))
        index += 1
    return found


def _assign_locants(parent: str, found: list[tuple[int | None, str]]) -> list[tuple[int, str]] | None:
    _, locants, mono_ok = PARENTS[parent]
    used: set[int] = set()
    placed: list[tuple[int, str]] = []
    pending: list[str] = []
    for locant, key in found:
        if locant is None:
            pending.append(key)
            continue
        if locant not in locants or locant in used:
            return None
        used.add(locant)
        placed.append((locant, key))
    if not pending:
        return placed
    # Only carbon sites are filled in automatically. Locant 1 of pyridine is
    # nitrogen, so pentachloropyridine lands on the five CH positions.
    smiles, _, _ = PARENTS[parent]
    parent_mol = _mol(smiles)
    free = [
        locant
        for locant, atom_idx in locants.items()
        if locant not in used and parent_mol.GetAtomWithIdx(atom_idx).GetAtomicNum() == 6
    ]
    if len(pending) == 1 and mono_ok and free:
        placed.append((free[0], pending[0]))
        return placed
    if len(pending) == len(free) and free and len(set(pending)) == 1:
        for locant, key in zip(free, pending):
            placed.append((locant, key))
        return placed
    return None


def parse_organic(name: str) -> str | None:
    """Turn a systematic name into SMILES, or return None when the name is ambiguous."""
    cleaned = re.sub(r"[\s_]+", "", name).lower()
    parent = next((key for key in sorted(PARENTS, key=len, reverse=True) if cleaned.endswith(key)), None)
    if parent is None:
        return None
    prefix = cleaned[: -len(parent)]
    found = _parse_substitutions(prefix)
    if found is None:
        return None
    if parent != "phosphine" and not found and prefix:
        return None
    assigned = _assign_locants(parent, found)
    if assigned is None:
        return None
    mol = _build_ring(parent, assigned)
    if mol is None:
        return None
    return Chem.MolToSmiles(mol)


def _consume_phosphine_groups(prefix: str) -> list[str] | None:
    text = prefix.lower()
    text = text.replace("tert-butyl", "tertbutyl").replace("t-butyl", "tertbutyl")
    text = text.replace("sec-butyl", "secbutyl").replace("i-propyl", "isopropyl")
    text = text.replace("n-butyl", "butyl").replace("n-propyl", "propyl")
    text = re.sub(r"[\s\-]+", "", text)
    groups: list[str] = []
    while text:
        group_key = next((key for key in _GROUP_KEYS if text.startswith(key)), None)
        multiplied: tuple[str, int, str] | None = None
        for mult, count in MULTIPLIERS:
            if not text.startswith(mult):
                continue
            rest = text[len(mult):]
            key = next((item for item in _GROUP_KEYS if rest.startswith(item)), None)
            if key is not None:
                multiplied = (key, count, mult + key)
                break
        group_len = len(group_key) if group_key else 0
        mult_len = len(multiplied[2]) if multiplied else 0
        if group_key and group_len >= mult_len:
            groups.append(group_key)
            text = text[group_len:]
        elif multiplied:
            groups.extend([multiplied[0]] * multiplied[1])
            text = text[mult_len:]
        else:
            return None
    return groups


def parse_phosphine_name(name: str) -> str | None:
    cleaned = name.lower().replace(" ", "")
    if not cleaned.endswith("phosphine"):
        return None
    prefix = cleaned[: -len("phosphine")]
    if prefix == "":
        return None
    groups = _consume_phosphine_groups(prefix)
    if not groups or len(groups) > 3:
        return None
    return _phosphine_smiles(groups)


def _phosphine_smiles(groups: list[str]) -> str | None:
    if not groups or len(groups) > 3:
        return None
    mol = _mol("P")
    if mol is None:
        return None
    for key in groups:
        mol = attach_group(mol, 0, GROUPS[key])
        if mol is None:
            return None
    return Chem.MolToSmiles(mol)


_FORMULA_TOKENS = (
    ("t-Bu", "tertbutyl"),
    ("tBu", "tertbutyl"),
    ("i-Pr", "isopropyl"),
    ("iPr", "isopropyl"),
    ("n-Bu", "butyl"),
    ("nBu", "butyl"),
    ("Oi-Pr", "isopropoxy"),
    ("OiPr", "isopropoxy"),
    ("OMe", "methoxy"),
    ("OEt", "ethoxy"),
    ("OPh", "phenoxy"),
    ("Cyp", "cyclopentyl"),
    ("Cy", "cyclohexyl"),
    ("Ph", "phenyl"),
    ("Me", "methyl"),
    ("Et", "ethyl"),
)


def _consume_formula_group(text: str, index: int) -> tuple[str, int, int] | None:
    if index < len(text) and text[index] == "(":
        close = text.find(")", index + 1)
        if close == -1:
            return None
        inner = text[index + 1:close]
        key = next((group for token, group in _FORMULA_TOKENS if token == inner), None)
        if key is None:
            return None
        cursor = close + 1
    else:
        token = next((item for item, _group in _FORMULA_TOKENS if text.startswith(item, index)), None)
        if token is None:
            return None
        key = dict(_FORMULA_TOKENS)[token]
        cursor = index + len(token)
    count = 1
    if cursor < len(text) and text[cursor] in "123":
        count = int(text[cursor])
        cursor += 1
    return key, count, cursor


def find_phosphine_formulas(text: str) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    for match in re.finditer(r"(?<![A-Za-z])P(?=[A-Z(tO])", text):
        cursor = match.end()
        groups: list[str] = []
        while True:
            step = _consume_formula_group(text, cursor)
            if step is None:
                break
            key, count, cursor = step
            groups.extend([key] * count)
        # PR3 only. PMe2 and PPh2 in these papers are fragments, not free phosphines.
        if len(groups) != 3:
            continue
        if cursor < len(text) and text[cursor].isalpha():
            continue
        smiles = _phosphine_smiles(groups)
        if smiles:
            found.append((match.start(), cursor, smiles))
    return found


def _entry(surface: str, smiles: str, kind: str, note: str = "", case_sensitive: bool = False) -> dict:
    mol = _mol(smiles)
    if mol is None:
        raise ValueError(f"bad SMILES for {surface}: {smiles}")
    canonical = Chem.MolToSmiles(mol)
    return {
        "surface": surface,
        "smiles": canonical,
        "kind": kind,
        "note": note,
        "case": case_sensitive,
        "pattern": re.compile(
            r"(?<![A-Za-z0-9\-])" + re.escape(surface) + r"(?![A-Za-z0-9])",
            0 if case_sensitive else re.IGNORECASE,
        ),
    }


def _lexicon() -> list[dict]:
    rows = [
        ("triphenylphosphine", "c1ccc(P(c2ccccc2)c2ccccc2)cc1", "ligand"),
        ("tricyclohexylphosphine", "C1CCC(P(C2CCCCC2)C2CCCCC2)CC1", "ligand"),
        ("trimethylphosphine", "CP(C)C", "ligand"),
        ("triethylphosphine", "CCP(CC)CC", "ligand"),
        ("trimethyl phosphite", "COP(OC)OC", "ligand"),
        ("tri-tert-butylphosphine", "CC(C)(C)P(C(C)(C)C)C(C)(C)C", "ligand"),
        ("tritert-butylphosphine", "CC(C)(C)P(C(C)(C)C)C(C)(C)C", "ligand"),
        (
            "tert-butylphosphine",
            "CC(C)(C)P(C(C)(C)C)C(C)(C)C",
            "ligand",
            "These papers pair this name with PtBu3, so the structure drawn is P(t-Bu)3.",
        ),
        ("cyclohexyl diphenylphosphine", "C1CCC(P(c2ccccc2)c2ccccc2)CC1", "ligand"),
        ("cyclohexyldiphenylphosphine", "C1CCC(P(c2ccccc2)c2ccccc2)CC1", "ligand"),
        ("dicyclohexylphenylphosphine", "C1CCC(P(C2CCCCC2)c2ccccc2)CC1", "ligand"),
        ("cyclopentyl diphenylphosphine", "C1CCC(P(c2ccccc2)c2ccccc2)C1", "ligand"),
        ("cyclopentyldiphenylphosphine", "C1CCC(P(c2ccccc2)c2ccccc2)C1", "ligand"),
        ("dicyclopentylphenylphosphine", "C1CCC(P(C2CCCC2)c2ccccc2)C1", "ligand"),
        ("benzaldehyde dimethyl acetal", "COC(OC)c1ccccc1", "molecule"),
        ("phenylboronic acid", "OB(O)c1ccccc1", "molecule"),
        ("4-fluorophenylboronic acid", "OB(O)c1ccc(F)cc1", "molecule"),
        (
            "para-fluorophenyl boroxine",
            "Fc1ccc(B2OB(c3ccc(F)cc3)OB(c3ccc(F)cc3)O2)cc1",
            "molecule",
            "The boroxine is the cyclic anhydride of 4-fluorophenylboronic acid.",
        ),
        ("para-fluorophenylboronic acid", "OB(O)c1ccc(F)cc1", "molecule"),
        ("benzyl alcohol", "OCc1ccccc1", "molecule"),
        ("benzyl alkoxide", "[O-]Cc1ccccc1", "molecule"),
        ("benzenethiol", "Sc1ccccc1", "molecule"),
        ("thiophenol", "Sc1ccccc1", "molecule"),
        ("piperidine", "C1CCNCC1", "molecule"),
        ("morpholine", "C1COCCN1", "molecule"),
        ("pyridine", "c1ccncc1", "molecule"),
        ("benzene", "c1ccccc1", "molecule"),
        ("quinoline", "c1ccc2ncccc2c1", "molecule"),
        ("naphthalene", "c1ccc2ccccc2c1", "molecule"),
        ("pyrimidine", "c1cncnc1", "molecule"),
        ("imidazole", "c1ncc[nH]1", "molecule"),
        ("pyrazole", "c1cn[nH]c1", "molecule"),
        ("pyrrole", "c1cc[nH]c1", "molecule"),
        ("thiophene", "c1ccsc1", "molecule"),
        ("furan", "c1ccoc1", "molecule"),
        ("oxazole", "c1cocn1", "molecule"),
        ("thiazole", "c1cscn1", "molecule"),
        ("indole", "c1ccc2[nH]ccc2c1", "molecule"),
        ("phenol", "Oc1ccccc1", "molecule"),
        ("aniline", "Nc1ccccc1", "molecule"),
        ("anisole", "COc1ccccc1", "molecule"),
        ("benzaldehyde", "O=Cc1ccccc1", "molecule"),
        ("chlorobenzene", "Clc1ccccc1", "molecule"),
        ("methoxide", "C[O-]", "molecule"),
        ("propene", "CC=C", "molecule"),
        ("styrene", "C=Cc1ccccc1", "molecule"),
        ("iodobenzene", "Ic1ccccc1", "molecule"),
        ("bromobenzene", "Brc1ccccc1", "molecule"),
        ("fluorobenzene", "Fc1ccccc1", "molecule"),
        ("acetophenone", "CC(=O)c1ccccc1", "molecule"),
        ("benzonitrile", "N#Cc1ccccc1", "molecule"),
        ("benzoic acid", "O=C(O)c1ccccc1", "molecule"),
        ("biphenyl", "c1ccc(-c2ccccc2)cc1", "molecule"),
        ("1-naphthol", "Oc1cccc2ccccc12", "molecule"),
        ("2-naphthol", "Oc1ccc2ccccc2c1", "molecule"),
        ("phenylacetylene", "C#Cc1ccccc1", "molecule"),
        ("mesitylene", "Cc1cc(C)cc(C)c1", "molecule"),
        ("benzophenone", "O=C(c1ccccc1)c1ccccc1", "molecule"),
        ("benzylamine", "NCc1ccccc1", "molecule"),
        ("phenylacetic acid", "O=C(O)Cc1ccccc1", "molecule"),
        ("BINOL", "Oc1ccc2ccccc2c1-c1c(O)ccc2ccccc12", "ligand", "", True),
        ("1,1'-bi-2-naphthol", "Oc1ccc2ccccc2c1-c1c(O)ccc2ccccc12", "ligand"),
        ("1,1′-bis-2-naphthol", "Oc1ccc2ccccc2c1-c1c(O)ccc2ccccc12", "ligand"),
        (
            "BINOL phosphoric acid",
            "O=P1(O)Oc2ccc3ccccc3c2-c2c(ccc3ccccc23)O1",
            "ligand",
        ),
        (
            "N-heterocyclic carbene",
            "CN1[C]N(C)C=C1",
            "ligand",
            "A generic imidazol-2-ylidene. A named NHC such as IMes is drawn in full when the paper names it.",
        ),
        (
            "N-heterocyclic carbenes",
            "CN1[C]N(C)C=C1",
            "ligand",
            "A generic imidazol-2-ylidene.",
        ),
        (
            "IMes",
            "Cc1cc(C)c(N2[C]N(c3c(C)cc(C)cc3C)C=C2)c(C)c1",
            "ligand",
            "1,3-bis(2,4,6-trimethylphenyl)imidazol-2-ylidene",
            True,
        ),
        (
            "SIMes",
            "Cc1cc(C)c(N2[C]N(c3c(C)cc(C)cc3C)CC2)c(C)c1",
            "ligand",
            "1,3-bis(2,4,6-trimethylphenyl)-4,5-dihydroimidazol-2-ylidene",
            True,
        ),
        (
            "IPr",
            "CC(C)c1cccc(C(C)C)c1N1[C]N(c2c(C(C)C)cccc2C(C)C)C=C1",
            "ligand",
            "1,3-bis(2,6-diisopropylphenyl)imidazol-2-ylidene",
            True,
        ),
        (
            "1,3-bis(2,4,6-trimethylphenyl)imidazol-2-ylidene",
            "Cc1cc(C)c(N2[C]N(c3c(C)cc(C)cc3C)C=C2)c(C)c1",
            "ligand",
        ),
        (
            "1,3-bis(2,4,6-trimethylphenyl)-4,5-dihydroimidazol-2-ylidene",
            "Cc1cc(C)c(N2[C]N(c3c(C)cc(C)cc3C)CC2)c(C)c1",
            "ligand",
        ),
        (
            "1,3-dicyclohexylimidazol-2-ylidene",
            "[C]1N(C2CCCCC2)C=CN1C1CCCCC1",
            "ligand",
        ),
        ("Cp*", "CC1=C(C)C(C)C(C)=C1C", "ligand", "Drawn as pentamethylcyclopentadiene, the protonated form of Cp*.", True),
        ("ferrocene", "[Fe+2].[cH-]1cccc1.[cH-]1cccc1", "molecule"),
        ("ferrocenyl", "[Fe+2].[cH-]1cccc1.[cH-]1cccc1", "group", "Ferrocenyl, drawn as ferrocene.", True),
        (
            "bis-oxazoline",
            "c1ccc(C2=NC(C3=NC(c4ccccc4)CO3)CO2)cc1",
            "ligand",
            "A phenyl-substituted BOX ligand, the scaffold used in the bidentate-ligand set.",
        ),
        ("BOX", "c1ccc(C2=NC(C3=NC(c4ccccc4)CO3)CO2)cc1", "ligand", "Phenyl BOX scaffold.", True),
        (
            "pyridine-oxazoline",
            "n1ccccc1C1=NCCO1",
            "ligand",
            "Oxazoline sits on C2 of the pyridine.",
        ),
        ("PyOX", "n1ccccc1C1=NCCO1", "ligand", "Pyridine-oxazoline scaffold, oxazoline at C2.", True),
        (
            "phosphorus-oxazoline",
            "c1ccc(P(c2ccccc2)c2ccccc2)c(C2=NCCO2)c1",
            "ligand",
            "A PHOX-type phosphorus-oxazoline.",
        ),
        ("PhosOX", "c1ccc(P(c2ccccc2)c2ccccc2)c(C2=NCCO2)c1", "ligand", "Phosphorus-oxazoline scaffold.", True),
        ("PHOX", "c1ccc(P(c2ccccc2)c2ccccc2)c(C2=NCCO2)c1", "ligand", "", True),
        (
            "alpha-diimine",
            "CC(=Nc1ccccc1)C(=Nc1ccccc1)C",
            "ligand",
        ),
        ("diimine", "CC(=Nc1ccccc1)C(=Nc1ccccc1)C", "ligand"),
        ("BINAP", "c1ccc(P(c2ccccc2)c2cccc3ccccc23)c(-c2cccc3cccc(P(c4ccccc4)c4ccccc4)c23)c1", "ligand", "", True),
        ("toluene", "Cc1ccccc1", "solvent"),
        ("tetrahydrofuran", "C1CCOC1", "solvent"),
        ("dichloromethane", "ClCCl", "solvent"),
        ("acetonitrile", "CC#N", "solvent"),
        ("methanol", "CO", "solvent"),
        ("ethanol", "CCO", "solvent"),
        ("acetone", "CC(C)=O", "solvent"),
        ("hexane", "CCCCCC", "solvent"),
        ("ethyl acetate", "CCOC(C)=O", "solvent"),
        ("diethyl ether", "CCOCC", "solvent"),
        ("THF", "C1CCOC1", "solvent", "", True),
        ("DCM", "ClCCl", "solvent", "", True),
        ("DMSO", "CS(C)=O", "solvent", "", True),
        ("DMF", "CN(C)C=O", "solvent", "", True),
        ("Me", "*C", "group", "Methyl substituent.", True),
        ("Et", "*CC", "group", "Ethyl substituent.", True),
        ("Ph", "*c1ccccc1", "group", "Phenyl substituent.", True),
        ("Cy", "*C1CCCCC1", "group", "Cyclohexyl substituent.", True),
        ("Cyp", "*C1CCCC1", "group", "Cyclopentyl substituent.", True),
        ("t-Bu", "*C(C)(C)C", "group", "tert-Butyl substituent.", True),
        ("tBu", "*C(C)(C)C", "group", "tert-Butyl substituent.", True),
        ("i-Pr", "*C(C)C", "group", "Isopropyl substituent.", True),
        ("iPr", "*C(C)C", "group", "Isopropyl substituent.", True),
        ("Oi-Pr", "*OC(C)C", "group", "Isopropoxy substituent.", True),
        ("n-Bu", "*CCCC", "group", "n-Butyl substituent.", True),
        ("n-Pr", "*CCC", "group", "n-Propyl substituent.", True),
        ("n-pentyl", "*CCCCC", "group", "Substituent drawn from the point of attachment."),
        ("n-butyl", "*CCCC", "group", "Substituent drawn from the point of attachment."),
        ("i-propyl", "*C(C)C", "group", "Substituent drawn from the point of attachment."),
        ("isopropyl", "*C(C)C", "group"),
        ("tert-butyl", "*C(C)(C)C", "group"),
        ("Ar", "*c1ccccc1", "group", "Aryl, drawn as phenyl.", True),
        ("Bz", "*Cc1ccccc1", "group", "Benzyl substituent.", True),
        ("Cp", "*C1C=CC=C1", "group", "Cyclopentadienyl substituent.", True),
        ("Fc", "[Fe+2].[cH-]1cccc1.[cH-]1cccc1", "group", "Ferrocenyl, drawn as ferrocene.", True),
        ("pyrazine", "c1cnccn1", "molecule"),
        ("pyridazine", "c1ccnnc1", "molecule"),
        ("p-toluenesulfonamide", "Cc1ccc(S(=O)(=O)N)cc1", "molecule"),
        ("4-methylbenzenesulfonamide", "Cc1ccc(S(=O)(=O)N)cc1", "molecule"),
        ("dimethylacetamide", "CC(=O)N(C)C", "molecule"),
        ("bis(pinacolato)diboron", "CC1(C)OB(B2OC(C)(C)C(C)(C)O2)OC1(C)C", "molecule"),
        ("2,2,6,6-tetramethylpiperidine-1-oxyl", "CC1(C)CCCC(C)(C)[N+]([O-])1", "molecule"),
        ("TsNH2", "Cc1ccc(S(=O)(=O)N)cc1", "molecule", "p-Toluenesulfonamide.", True),
        ("TEMPO", "CC1(C)CCCC(C)(C)[N+]([O-])1", "molecule", "2,2,6,6-Tetramethylpiperidine-1-oxyl.", True),
        ("B2pin2", "CC1(C)OB(B2OC(C)(C)C(C)(C)O2)OC1(C)C", "molecule", "Bis(pinacolato)diboron.", True),
        ("TMSCl", "C[Si](C)(C)Cl", "molecule", "Chlorotrimethylsilane.", True),
        ("Et3N", "CCN(CC)CC", "molecule", "Triethylamine."),
        ("Et3N.HI", "CC[NH+](CC)CC.[I-]", "molecule", "Triethylamine hydroiodide."),
        ("NiBr2.glyme", "COCCOC.[Ni+2].[Br-].[Br-]", "molecule", "Nickel bromide glyme complex."),
        ("Fc/Fc+", "[Fe+2].[cH-]1cccc1.[cH-]1cccc1", "molecule", "The Fc/Fc+ couple, drawn as ferrocene.", True),
    ]
    entries = []
    for row in rows:
        surface, smiles, kind = row[0], row[1], row[2]
        note = row[3] if len(row) > 3 else ""
        case_sensitive = row[4] if len(row) > 4 else False
        entries.append(_entry(surface, smiles, kind, note, case_sensitive))
    entries.sort(key=lambda item: len(item["surface"]), reverse=True)
    return entries


LEXICON = _lexicon()


def _bundle(entries: list[dict], ignore_case: bool) -> tuple[re.Pattern[str] | None, dict[str, dict]]:
    if not entries:
        return None, {}
    ordered = sorted(entries, key=lambda entry: len(entry["surface"]), reverse=True)
    pattern = re.compile(
        r"(?<![A-Za-z0-9\-])(" + "|".join(re.escape(entry["surface"]) for entry in ordered) + r")(?![A-Za-z0-9])",
        re.IGNORECASE if ignore_case else 0,
    )
    lookup: dict[str, dict] = {}
    for entry in ordered:
        lookup.setdefault(entry["surface"].lower() if ignore_case else entry["surface"], entry)
    return pattern, lookup


_LEXICON_FOLDED = _bundle([entry for entry in LEXICON if not entry["case"]], True)
_LEXICON_EXACT = _bundle([entry for entry in LEXICON if entry["case"]], False)
_PARENT_NAMES = (
    "benzenesulfonamide|aziridine|benzene|pyridine|pyrimidine|quinoline|"
    "naphthalene|phosphine|aniline|phenol|benzaldehyde|benzonitrile"
)
_PARENT_HINT = re.compile(_PARENT_NAMES, re.IGNORECASE)

# Names the grammar would mis-read because the paper's wording is not the strict IUPAC name.
_LEXICON_OVERRIDE = {
    entry["surface"].lower()
    for entry in LEXICON
    if entry["kind"] in {"ligand", "molecule"}
}


def _structure_for(smiles: str, title: str, kind: str, note: str = "") -> Structure:
    mol = _mol(smiles)
    if mol is None:
        raise ValueError(smiles)
    canonical = Chem.MolToSmiles(mol)
    return Structure(smiles=canonical, title=title, kind=kind, note=note, mid=canonical)


def normalize_chemical(text: str) -> str:
    """Fold the spellings a PDF uses into the spellings the names expect.

    Subscripts, superscripts and unicode dashes are one character either way,
    so a word's box still lines up after the fold.
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789"))
    text = text.translate(str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789"))
    text = text.replace("\u00ad", "")
    for dash in ("‐", "‑", "‒", "–", "—", "―"):
        text = text.replace(dash, "-")
    for bullet in ("\u00b7", "\u2022", "\u2219", "\u22c5"):
        text = text.replace(bullet, ".")
    text = text.replace("\u207a", "+").replace("\u207b", "-")
    return text


_LABEL_STOP = {
    "figure", "fig", "scheme", "table", "panel", "the", "and", "for", "with",
    "from", "this", "that", "ligand", "ligands",
}


def _good_label(label: str) -> bool:
    """A paper's own short name: L1, Ligand 2, IMes, BrettPhos, not an ordinary word."""
    if label.lower() in _LABEL_STOP:
        return False
    if re.fullmatch(r"L\d{1,2}[a-z]?", label, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"Ligand\s+\d{1,2}", label, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"\d{1,3}[a-z]{1,3}", label, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"[A-Z][A-Za-z0-9]{1,12}", label) and any(char.isdigit() for char in label):
        return True
    if re.fullmatch(r"[A-Z]{2,8}", label):
        return True
    return bool(re.fullmatch(r"[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+", label))


def aliases_near(text: str, spans: list[Span]) -> dict[str, Structure]:
    """Read 'name (L1)' and 'L1 = name' so the paper's own label can be drawn later."""
    found: dict[str, Structure] = {}
    for span in spans:
        if span.structure.kind not in {"molecule", "ligand"}:
            continue
        before = text[max(0, span.start - 28):span.start]
        after = text[span.end:span.end + 28]
        patterns = (
            re.match(r"\s*\(\s*([A-Za-z0-9][\w.+\-]{0,24})\s*\)", after),
            re.search(r"([A-Za-z0-9][\w.+\-]{0,24})\s*\(\s*$", before),
            re.search(r"\b(L\d{1,2}[a-z]?)\s*[=:]\s*$", before, flags=re.IGNORECASE),
            re.search(r"\b(Ligand\s+\d{1,2})\s*[=:,]?\s*$", before, flags=re.IGNORECASE),
        )
        for match in patterns:
            if match is None:
                continue
            label = match.group(1)
            if _good_label(label) and label.lower() != span.surface.lower():
                found[label] = span.structure
    return found


_ALIAS_RE: dict[tuple[str, int], re.Pattern[str]] = {}


def _alias_hits(text: str, extra: dict[str, Structure]) -> list[Span]:
    spans: list[Span] = []
    for label, structure in extra.items():
        flags = re.IGNORECASE if re.fullmatch(
            r"L\d{1,2}[a-z]?|Ligand\s+\d{1,2}",
            label,
            flags=re.IGNORECASE,
        ) else 0
        key = (label, flags)
        pattern = _ALIAS_RE.get(key)
        if pattern is None:
            pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(label) + r"(?![A-Za-z0-9])", flags)
            _ALIAS_RE[key] = pattern
        for match in pattern.finditer(text):
            spans.append(Span(match.start(), match.end(), match.group(0), structure, 0))
    return spans


def apply_aliases(text: str, spans: list[Span], extra: dict[str, Structure]) -> list[Span]:
    """Add labels learned elsewhere in the paper, without scanning the name list again."""
    if not extra:
        return spans
    return _select([*spans, *_alias_hits(text, extra)])


def find_spans(text: str, extra: dict[str, Structure] | None = None) -> list[Span]:
    spans: list[Span] = []
    for pattern, lookup, fold in (
        (*_LEXICON_FOLDED, True),
        (*_LEXICON_EXACT, False),
    ):
        if pattern is None:
            continue
        for match in pattern.finditer(text):
            token = match.group(1)
            entry = lookup.get(token.lower() if fold else token)
            if entry is None:
                continue
            spans.append(
                Span(
                    match.start(),
                    match.end(),
                    match.group(0),
                    _structure_for(entry["smiles"], entry["surface"], entry["kind"], entry["note"]),
                    0,
                )
            )
    for match in re.finditer(r"(?<![A-Za-z0-9\-])Et3N(?![A-Za-z])", text, flags=re.IGNORECASE):
        entry = next(item for item in LEXICON if item["surface"] == "Et3N")
        spans.append(
            Span(
                match.start(),
                match.end(),
                match.group(0),
                _structure_for(entry["smiles"], entry["surface"], entry["kind"], entry["note"]),
                0,
            )
        )
    for start, end, smiles in find_phosphine_formulas(text):
        surface = text[start:end]
        spans.append(Span(start, end, surface, _structure_for(smiles, surface, "ligand"), 1))

    if _PARENT_HINT.search(text):
        for match in _PARENT_RE.finditer(text):
            parent = match.group(2).lower()
            prefix = match.group(1)
            if not prefix:
                continue
            parts = _split_prefix(prefix)
            for offset in range(len(parts)):
                candidate = "-".join(parts[offset:]) + parent
                if parent == "phosphine":
                    smiles = parse_phosphine_name(candidate)
                    kind = "ligand"
                else:
                    smiles = parse_organic(candidate)
                    kind = "molecule"
                if not smiles:
                    continue
                if offset:
                    start = match.start() + len("-".join(parts[:offset])) + 1
                else:
                    start = match.start()
                # A tail of a longer name that did not parse is not that name.
                if start > 0 and text[start - 1] in "-/":
                    continue
                surface = text[start:match.end()]
                if surface.lower() in _LEXICON_OVERRIDE:
                    break
                spans.append(Span(start, match.end(), surface, _structure_for(smiles, surface, kind), 2))
                break
    if extra:
        spans.extend(_alias_hits(text, extra))
    return _select(spans)


def _select(spans: list[Span]) -> list[Span]:
    spans.sort(key=lambda span: (-(span.end - span.start), span.priority, span.start))
    chosen: list[Span] = []
    for span in spans:
        if any(span.start < other.end and other.start < span.end for other in chosen):
            continue
        chosen.append(span)
    chosen.sort(key=lambda span: span.start)
    return chosen


_PARENT_RE = re.compile(
    r"(?<![A-Za-z0-9\-])([0-9A-Za-z,()\-]*?)(" + _PARENT_NAMES + r")(?![A-Za-z])",
    re.IGNORECASE,
)

_CITE = re.compile(
    r"(?<=[a-z)])(?<!sp)(\d{1,3}(?:[–—,\-]\d{1,3})*)(?![\d%A-Za-z])"
)


def find_citations(text: str, spans: list[Span]) -> list[tuple[int, int, str]]:
    blocked = [(span.start, span.end) for span in spans]
    cites: list[tuple[int, int, str]] = []
    for match in _CITE.finditer(text):
        if any(match.start() < end and start < match.end() for start, end in blocked):
            continue
        cites.append((match.start(), match.end(), match.group(1)))
    return cites


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u00ad", "")
    text = text.replace("\f", "\n\n")
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if "Downloaded from" in line:
            continue
        if re.fullmatch(r"\d{1,4}", stripped):
            continue
        if stripped.lower() in {"article", "articles", "edge article"}:
            continue
        kept.append(line.rstrip())
    text = "\n".join(kept)
    text = re.sub(r"([A-Za-z])-\n\s*([a-z])", r"\1\2", text)
    text = re.sub(
        rf"(?<=\d)(?=(?:di|tri|tetra|penta|hexa|bis|tris)?(?:{_SUBST_WORD}))",
        "-",
        text,
        flags=re.IGNORECASE,
    )
    return text


def split_references(text: str) -> tuple[str, str]:
    matches = list(re.finditer(r"(?im)^[ \t]*references\b[^\n]*$", text))
    if matches:
        cut = matches[-1]
        return text[: cut.start()], text[cut.end():]
    # Some extracts (RSC in particular) have no heading. The list starts
    # "1 R. C. Larock" and the next entry is "2 ...".
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    first = re.compile(r"^\s*1\s+(?:[A-Z]\.\s*)+[A-Z]")
    second = re.compile(r"^\s*2\s+(?:[A-Z]\.\s*)+[A-Z]")
    for index, line in enumerate(lines):
        if not first.match(line):
            continue
        window = lines[index:index + 10]
        if any(second.match(item) for item in window):
            return text[: offsets[index]], text[offsets[index]:]
    return text, ""


def paragraphs(text: str) -> list[str]:
    chunks = re.split(r"\n\s*\n", text)
    output: list[str] = []
    for chunk in chunks:
        chunk = re.sub(r"[ \t]*\n[ \t]*", " ", chunk)
        chunk = re.sub(r" +", " ", chunk).strip()
        if chunk:
            output.append(chunk)
    return output


_REF_START = re.compile(r"^\s*(?:\((\d{1,3})\)|(\d{1,3})\.)\s*(.*)$")
_REF_BARE = re.compile(r"^\s*\((\d{1,3})\)$")
_REF_START_LOOSE = re.compile(r"^\s*(\d{1,3})\s+([A-Z][\w.''\-]+.*\S)\s*$")


def _join_ref(lines: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def parse_references(text: str) -> list[tuple[str, str]]:
    """Pair reference numbers with their citation text.

    Two layouts show up in these extracts. ACS/Nature put the number and
    the citation on the same line. The Sterimol extract dumps a column of
    bare numbers, then the citations underneath; those are zipped back together.
    """
    raw_lines = [line.strip() for line in text.splitlines()]
    raw_lines = [line for line in raw_lines if line and not re.fullmatch(r"\d{1,4}", line)]
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(raw_lines):
        line = raw_lines[index]
        if _REF_BARE.match(line) and index + 1 < len(raw_lines) and _REF_BARE.match(raw_lines[index + 1]):
            numbers: list[str] = []
            while index < len(raw_lines) and (bare := _REF_BARE.match(raw_lines[index])):
                numbers.append(bare.group(1))
                index += 1
            bodies: list[str] = []
            while index < len(raw_lines) and not _REF_BARE.match(raw_lines[index]):
                bodies.append(raw_lines[index])
                index += 1
            for number, body in zip(numbers, bodies):
                entries.append((number, body))
            continue
        strict = _REF_START.match(line)
        loose = _REF_START_LOOSE.match(line) if strict is None else None
        if strict or loose:
            if strict:
                number = strict.group(1) or strict.group(2)
                body_lines = [strict.group(3)] if strict.group(3).strip() else []
            else:
                number = loose.group(1)
                body_lines = [loose.group(2)]
            index += 1
            while index < len(raw_lines):
                nxt = raw_lines[index]
                if _REF_START.match(nxt) or _REF_START_LOOSE.match(nxt) or _REF_BARE.match(nxt):
                    break
                body_lines.append(nxt)
                index += 1
            body = _join_ref(body_lines)
            if body:
                entries.append((number, body))
            continue
        index += 1
    return entries


def render_fragment(text: str, spans: list[Span], cites: list[tuple[int, int, str]], known_refs: set[str], inline: bool) -> str:
    events: list[tuple[int, int, str]] = [("mol", span.start, span.end, span) for span in spans]
    # rebuild as typed pieces
    pieces: list[tuple[int, int, str, object]] = []
    for span in spans:
        pieces.append((span.start, span.end, "mol", span))
    for start, end, label in cites:
        pieces.append((start, end, "cite", label))
    pieces.sort(key=lambda item: (item[0], item[1] - item[0]))
    # drop cite overlaps just in case
    filtered: list[tuple[int, int, str, object]] = []
    cursor_end = -1
    for start, end, kind, payload in pieces:
        if start < cursor_end:
            continue
        filtered.append((start, end, kind, payload))
        cursor_end = end

    seen_inline: set[str] = set()
    out: list[str] = []
    cursor = 0
    for start, end, kind, payload in filtered:
        out.append(html.escape(text[cursor:start]))
        surface = html.escape(text[start:end])
        if kind == "mol":
            span: Span = payload  # type: ignore[assignment]
            mid = html.escape(span.structure.mid, quote=True)
            out.append(f'<mark class="chem {span.structure.kind}" data-mid="{mid}">{surface}</mark>')
            if inline and span.structure.kind in {"molecule", "ligand"} and span.structure.mid not in seen_inline:
                seen_inline.add(span.structure.mid)
                out.append(f'<span class="inline-slot" data-mid="{mid}"></span>')
        else:
            label: str = payload  # type: ignore[assignment]
            links = []
            for number in re.findall(r"\d+", label):
                if number in known_refs:
                    links.append(f'<a href="#ref-{number}">{number}</a>')
                else:
                    links.append(number)
            # preserve separators from the original label roughly as commas
            joined = ",".join(links) if "," in label or "–" in label or "-" in label else "".join(links)
            if len(links) > 1 and ("," in label or "–" in label or "-" in label):
                sep = "–" if "–" in label or "-" in label else ","
                joined = sep.join(links)
            out.append(f'<sup class="cite">{joined}</sup>')
        cursor = end
    out.append(html.escape(text[cursor:]))
    return "".join(out)


_SVG_CACHE: dict[str, str] = {}


def depict_svg(smiles: str, width: int = 320, height: int = 200) -> str:
    cached = _SVG_CACHE.get(smiles)
    if cached is not None:
        return cached
    mol = _mol(smiles)
    if mol is None:
        return ""
    drawer = rdMolDraw2D.MolDraw2DSVG(width, height)
    options = drawer.drawOptions()
    options.clearBackground = False
    options.bondLineWidth = 1.6
    options.padding = 0.08
    options.includeRadicals = False
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol)
    drawer.FinishDrawing()
    svg = drawer.GetDrawingText()
    svg = re.sub(r"<\?xml[^>]*>", "", svg)
    svg = re.sub(r"<!DOCTYPE[^>]*>", "", svg)
    svg = svg.strip()
    _SVG_CACHE[smiles] = svg
    return svg


_PNG_CACHE: dict[str, bytes] = {}


def depict_png(smiles: str, width: int = 360, height: int = 220) -> bytes:
    """White-background PNG for a PDF card. Empty bytes if the SMILES will not draw."""
    cached = _PNG_CACHE.get(smiles)
    if cached is not None:
        return cached
    mol = _mol(smiles)
    if mol is None:
        return b""
    drawer = rdMolDraw2D.MolDraw2DCairo(width, height)
    options = drawer.drawOptions()
    options.clearBackground = True
    options.bondLineWidth = 1.8
    options.padding = 0.1
    options.includeRadicals = False
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol)
    drawer.FinishDrawing()
    png = drawer.GetDrawingText()
    _PNG_CACHE[smiles] = png
    return png


def depict_reaction(reaction_smiles: str, width: int = 760, height: int = 220) -> str:
    reaction = rdChemReactions.ReactionFromSmarts(reaction_smiles, useSmiles=True)
    if reaction is None:
        return ""
    drawer = rdMolDraw2D.MolDraw2DSVG(width, height)
    options = drawer.drawOptions()
    options.clearBackground = False
    options.bondLineWidth = 1.5
    options.padding = 0.12
    options.includeRadicals = False
    drawer.DrawReaction(reaction)
    drawer.FinishDrawing()
    svg = drawer.GetDrawingText()
    svg = re.sub(r"<\?xml[^>]*>", "", svg)
    return svg.strip()


PAPER_TITLES = {
    "wu_doyle.txt": "Wu & Doyle, 2017 — Phosphines for a nickel Suzuki coupling",
    "d2sc04041g.txt": "Lu, Paci & Leitch, 2022 — Predicting SNAr reactivity",
    "buried_volume.txt": "Hillier, Cavallo & Nolan, 2003 — NHC binding to Cp*RuCl",
    "cone_angle.txt": "Tolman, 1977 — The steric cone angle of phosphorus ligands",
    "cone_angle_example.txt": "Hu & Ogasawara, 2021 — Ferrocenylphosphines",
    "20-8-18wSterimol_paper.txt": "Brethomé, Fletcher & Paton — Sterimol parameters of flexible groups",
    "smith_toste.txt": "Smith & Toste, 2026 — Buried volume of chiral acids",
    "cs3_paper.txt": "Schoepfer & Corminboeuf, 2024 — Featurizing bidentate ligands",
    "quadrants.txt": "Antinucci, Busico, Cavallo et al., 2022 — Quadrant buried volume",
    "may_refrence_verloop_origin.txt": "Alturaifi, Wang & Liu — Heteroaryl substituent descriptors",
    "taft.txt": "Taft extract — ester hydrolysis (noisy scan)",
}


def schemes_for(filename: str) -> list[Scheme]:
    library = {
        "wu_doyle.txt": [
            Scheme(
                title="The reaction the ligands were built for",
                caption="Nickel-catalysed coupling of benzaldehyde dimethyl acetal with a para-fluorophenyl boron reagent. One methoxy group is replaced by the aryl ring, giving a benzylic ether.",
                mode="reaction",
                reaction="COC(OC)c1ccccc1.OB(O)c1ccc(F)cc1>>COC(c1ccccc1)c2ccc(F)cc2",
                molecules=[
                    ("COC(OC)c1ccccc1", "benzaldehyde dimethyl acetal"),
                    ("OB(O)c1ccc(F)cc1", "4-fluorophenylboronic acid"),
                    ("COC(c1ccccc1)c2ccc(F)cc2", "benzylic ether"),
                ],
            )
        ],
        "d2sc04041g.txt": [
            Scheme(
                title="Touchstone SNAr",
                caption="Benzyl alkoxide displaces chloride from 2-chloropyridine. The paper’s rate scale is anchored on this reaction and two substituted pyridines.",
                mode="reaction",
                reaction="[O-]Cc1ccccc1.Clc1ccccn1>>c1ccc(COc2ccccn2)cc1",
                molecules=[
                    ("[O-]Cc1ccccc1", "benzyl alkoxide"),
                    ("Clc1ccccn1", "2-chloropyridine"),
                    ("c1ccc(COc2ccccn2)cc1", "2-(benzyloxy)pyridine"),
                ],
            )
        ],
        "buried_volume.txt": [
            Scheme(
                title="Ligands being compared",
                caption="Unsaturated IMes, saturated SIMes, and PCy3. Cp* is the pentamethylcyclopentadienyl ligand on ruthenium.",
                mode="comparison",
                molecules=[
                    ("Cc1cc(C)c(N2[C]N(c3c(C)cc(C)cc3C)C=C2)c(C)c1", "IMes"),
                    ("Cc1cc(C)c(N2[C]N(c3c(C)cc(C)cc3C)CC2)c(C)c1", "SIMes"),
                    ("C1CCC(P(C2CCCCC2)C2CCCCC2)CC1", "PCy3"),
                    ("CC1=C(C)C(C)C(C)=C1C", "Cp*H"),
                ],
            )
        ],
        "cone_angle.txt": [
            Scheme(
                title="Tolman cone-angle series",
                caption="The same phosphorus atom, four different substituents. Cone angle grows from trimethyl phosphite to tri-tert-butylphosphine.",
                mode="comparison",
                molecules=[
                    ("COP(OC)OC", "P(OMe)3 · small"),
                    ("CP(C)C", "PMe3"),
                    ("c1ccc(P(c2ccccc2)c2ccccc2)cc1", "PPh3"),
                    ("C1CCC(P(C2CCCCC2)C2CCCCC2)CC1", "PCy3"),
                    ("CC(C)(C)P(C(C)(C)C)C(C)(C)C", "P(t-Bu)3 · large"),
                ],
            )
        ],
        "cone_angle_example.txt": [
            Scheme(
                title="Ferrocenylphosphines",
                caption="The paper compares Fc3P, Fc2PhP and triphenylphosphine. Ferrocene is shown beside PPh3 so the substituent is recognizable.",
                mode="comparison",
                molecules=[
                    ("[Fe+2].[cH-]1cccc1.[cH-]1cccc1", "ferrocene (FcH)"),
                    ("c1ccc(P(c2ccccc2)c2ccccc2)cc1", "PPh3"),
                ],
            )
        ],
        "20-8-18wSterimol_paper.txt": [
            Scheme(
                title="Substituents Sterimol measures",
                caption="Each structure is drawn from the point of attachment (*). B1 and B5 are the short and long widths perpendicular to that bond; L runs along it. Flexible chains (n-butyl, n-pentyl) change shape with conformation, which is the point of the paper.",
                mode="comparison",
                molecules=[
                    ("*C", "methyl"),
                    ("*CC", "ethyl"),
                    ("*C(C)C", "i-propyl"),
                    ("*C(C)(C)C", "t-butyl"),
                    ("*CCCC", "n-butyl"),
                    ("*CCCCC", "n-pentyl"),
                    ("*c1ccccc1", "phenyl"),
                    ("*C1CCCCC1", "cyclohexyl"),
                ],
            )
        ],
        "smith_toste.txt": [
            Scheme(
                title="The catalyst scaffold",
                caption="Brønsted acid organocatalysts in this paper are built on the BINOL phosphoric acid scaffold. Buried volume is measured around that chiral pocket.",
                mode="comparison",
                molecules=[
                    ("Oc1ccc2ccccc2c1-c1c(O)ccc2ccccc12", "BINOL"),
                    ("O=P1(O)Oc2ccc3ccccc3c2-c2c(ccc3ccccc23)O1", "BINOL phosphoric acid"),
                ],
            )
        ],
        "cs3_paper.txt": [
            Scheme(
                title="Bidentate ligand classes in the data set",
                caption="Most of the reactions used bis-oxazolines. Pyridine-oxazolines, phosphorus-oxazolines and α-diimines are the other families named in the text.",
                mode="comparison",
                molecules=[
                    ("c1ccc(C2=NC(C3=NC(c4ccccc4)CO3)CO2)cc1", "bis-oxazoline"),
                    ("n1ccccc1C1=NCCO1", "pyridine-oxazoline"),
                    ("c1ccc(P(c2ccccc2)c2ccccc2)c(C2=NCCO2)c1", "phosphorus-oxazoline"),
                    ("CC(=Nc1ccccc1)C(=Nc1ccccc1)C", "α-diimine"),
                ],
            )
        ],
        "quadrants.txt": [
            Scheme(
                title="The two reactions used as tests",
                caption="Propene polymerization, and a thiol addition to an N-acyl imine catalysed by a BINOL phosphoric acid. Quadrant and octant buried volumes describe the catalyst, not these substrates.",
                mode="reaction",
                reaction="Sc1ccccc1.CC(=O)N=Cc1ccccc1>>CC(=O)NC(Sc1ccccc1)c2ccccc2",
                molecules=[
                    ("CC=C", "propene"),
                    ("Sc1ccccc1", "thiophenol"),
                    ("CC(=O)N=Cc1ccccc1", "N-acyl imine"),
                    ("O=P1(O)Oc2ccc3ccccc3c2-c2c(ccc3ccccc23)O1", "BINOL phosphoric acid"),
                ],
            )
        ],
        "may_refrence_verloop_origin.txt": [
            Scheme(
                title="Heteroaryl parents in the database",
                caption="HArD stores steric and electronic descriptors for substituents built on these rings, including every regioisomer.",
                mode="comparison",
                molecules=[
                    ("c1ccncc1", "pyridine"),
                    ("c1cncnc1", "pyrimidine"),
                    ("c1cnccn1", "pyrazine"),
                    ("c1ccnnc1", "pyridazine"),
                    ("c1ccsc1", "thiophene"),
                    ("c1ccoc1", "furan"),
                    ("c1cc[nH]c1", "pyrrole"),
                    ("c1ncc[nH]1", "imidazole"),
                ],
            )
        ],
    }
    return library.get(filename, [])


def annotate_document(raw: str, filename: str) -> dict:
    text = clean_text(raw)
    body, refs_raw = split_references(text)
    ref_entries = parse_references(refs_raw)
    known_refs = {number for number, _body in ref_entries}

    body_paragraphs = []
    molecules: dict[str, dict] = {}
    mention_count = 0

    def absorb(spans: list[Span]) -> None:
        nonlocal mention_count
        for span in spans:
            mention_count += 1
            record = molecules.get(span.structure.mid)
            if record is None:
                molecules[span.structure.mid] = {
                    "mid": span.structure.mid,
                    "smiles": span.structure.smiles,
                    "title": span.structure.title,
                    "kind": span.structure.kind,
                    "note": span.structure.note,
                    "count": 1,
                    "aliases": [span.surface],
                }
            else:
                record["count"] += 1
                if span.surface not in record["aliases"] and len(record["aliases"]) < 8:
                    record["aliases"].append(span.surface)

    for paragraph in paragraphs(body):
        spans = find_spans(paragraph)
        cites = find_citations(paragraph, spans)
        absorb(spans)
        body_paragraphs.append(
            "<p>" + render_fragment(paragraph, spans, cites, known_refs, inline=True) + "</p>"
        )

    ref_html: list[str] = []
    for number, ref_body in ref_entries:
        spans = find_spans(ref_body)
        absorb(spans)
        inner = render_fragment(ref_body, spans, [], known_refs, inline=False)
        mids = []
        for span in spans:
            if span.structure.kind == "solvent":
                continue
            if span.structure.mid not in mids:
                mids.append(span.structure.mid)
        thumbs = "".join(
            f'<span class="ref-thumb" data-mid="{html.escape(mid, quote=True)}"></span>' for mid in mids[:6]
        )
        thumb_row = f'<div class="ref-thumbs">{thumbs}</div>' if thumbs else ""
        ref_html.append(
            f'<li id="ref-{html.escape(number)}" class="ref"><span class="ref-num">{html.escape(number)}</span>'
            f'<div class="ref-body">{thumb_row}<div class="ref-text">{inner}</div></div></li>'
        )

    scheme_payload = []
    for scheme in schemes_for(filename):
        for smiles, title in scheme.molecules:
            mol = _mol(smiles)
            if mol is None:
                continue
            canonical = Chem.MolToSmiles(mol)
            if canonical not in molecules:
                molecules[canonical] = {
                    "mid": canonical,
                    "smiles": canonical,
                    "title": title,
                    "kind": "group" if canonical.startswith("*") or title.startswith("*") else "molecule",
                    "note": "Shown in the scheme for this paper.",
                    "count": 0,
                    "aliases": [title],
                }
        scheme_payload.append(
            {
                "title": scheme.title,
                "caption": scheme.caption,
                "mode": scheme.mode,
                "svg": depict_reaction(scheme.reaction) if scheme.mode == "reaction" and scheme.reaction else "",
                "molecules": [
                    {"smiles": Chem.MolToSmiles(_mol(smiles)), "name": title}
                    for smiles, title in scheme.molecules
                    if _mol(smiles) is not None
                ],
            }
        )

    ordered = sorted(molecules.values(), key=lambda item: (-(item["kind"] != "solvent"), item["title"].lower()))
    # appearance order is more useful than alpha; rebuild from insertion order
    ordered = list(molecules.values())

    return {
        "filename": filename,
        "title": PAPER_TITLES.get(filename, filename),
        "body_html": "\n".join(body_paragraphs),
        "refs_html": "\n".join(ref_html),
        "molecules": ordered,
        "schemes": scheme_payload,
        "stats": {
            "mentions": mention_count,
            "unique": len(molecules),
            "references": len(ref_entries),
        },
    }
