"""Custom entity types for biblical text extraction.

Each Pydantic model becomes a node label Graphiti can classify entities into; the
*class docstring* is what Graphiti feeds the LLM as the type's definition, so the
docstrings below are written as classification instructions, not human prose. Keep
them tight (they cost tokens on every chapter) but explicitly disambiguating —
the hard cases for a biblical corpus are Deity vs DivineBeing, Person vs Group vs
Title, and Event vs TimePeriod.

Field-less by design
--------------------
The models intentionally declare no fields. Graphiti's attribute-extraction step
(_extract_entity_attributes) returns early when a type has no fields, which avoids
it writing schema-shaped Map values into Neo4j node properties (Neo4j only allows
primitives/arrays). The cost of this is real: we lose rich attributes that would be
queryable later — dates, roles, gender, the meaning of a name, a place's region.
When that path is reactivated, the usual fix is to serialize complex attributes as a
JSON string or flatten them into primitive properties before writing.

What is NOT an entity type (model as a relation or attribute instead)
--------------------------------------------------------------------
These recur constantly in the text and would explode the node count for little query
value. They belong in the edge_type_map / as fact attributes, not here — the relevant
restraints are echoed in the class docstrings below to steer the LLM at extraction:
  * Kinship / genealogy ("son of", "father of") -> relation (PARENT_OF, MARRIED_TO; see EDGE_TYPES).
  * Numbers / quantities ("forty days", "twelve tribes") -> attribute of a fact.
  * Divine names / attributes ("the Almighty", "Emmanuel") -> aliases of the Deity node.
  * Prophecies / blessings / curses / individual laws -> relations with attributes
    (BLESSED, COMMANDED, CURSED; see EDGE_TYPES), not Event nodes.

Deferred to a later version
---------------------------
Concept / Theme (sin, grace, covenant-as-idea, faith, law, salvation) is deliberately
left out of v1: it is the highest-noise type to extract with gpt-4o-mini. Add it only
after measuring that thematic RAG needs it, and back it with a controlled vocabulary.
"""
from __future__ import annotations

from pydantic import BaseModel


class Person(BaseModel):
    """A named human individual: patriarch, prophet, priest, king, judge, apostle, etc.

    Not a deity or spiritual being (use Deity or DivineBeing), not a collective people
    (use Group), and not a non-human creature (use Creature). A person's office or rank
    (king, prophet) is a Title, not part of this entity. Kinship such as "son of" or
    "father of" is a relation between two Persons, not an entity.
    """


class Group(BaseModel):
    """A collective of people: nation, tribe, family line, people, or organized body.

    Use for plural peoples (Israelites, Philistines, Levites, the twelve tribes, the
    Sanhedrin), not a single person (use Person) and not a herd/swarm of animals
    (use Creature).
    """


class Place(BaseModel):
    """A geographic location: city, town, land, region, river, mountain, sea, or body of water.

    The temple or tabernacle as a physical site is a Place; a portable object such as the
    ark is an Artifact, not a Place.
    """


class Deity(BaseModel):
    """The God of Israel only: God, the LORD, YHWH, the LORD God, the Almighty, the Most High.

    Treat "LORD", "LORD God", "God", and "the Spirit of God" / "the Holy Spirit" as the
    SAME single entity. Divine names and titles ("the Almighty", "Emmanuel", "the Holy One
    of Israel") are ALIASES of this one node, never new entities. Angels, Satan, and false
    gods are NOT Deity — use DivineBeing for those.
    """


class DivineBeing(BaseModel):
    """A non-supreme spiritual being, distinct from the God of Israel (use Deity) and from
    humans (use Person):

    angel, archangel, cherub, seraph, the angel of the LORD, Satan / the devil, a demon, or
    a foreign / false god (Baal, Dagon, Molech, Asherah, Chemosh, Ashtoreth).
    """


class Creature(BaseModel):
    """A non-human living being with a narrative role: animal, beast, bird, fish, or symbolic
    creature.

    Examples: the serpent of Genesis 3, Balaam's donkey, the great fish of Jonah, Daniel's
    lions, the living creatures of Ezekiel and Revelation. Not a human (use Person) and not a
    divine or spiritual being (use Deity or DivineBeing).
    """


class Plant(BaseModel):
    """A specific, named or narratively significant tree, plant, or vegetation.

    Use for individual plants that carry narrative or symbolic weight: the Tree of Life, the
    Tree of Knowledge of Good and Evil, the burning bush, Jonah's gourd, the vine of John 15.
    A garden or field as a location is a Place, not a Plant; a fashioned wooden object (an
    ark, a staff) is an Artifact. Generic vegetation mentioned only in passing ("herb",
    "grass") is not an entity.
    """


class Artifact(BaseModel):
    """A notable physical object: ark, tabernacle furnishing, altar, idol, scroll, tablets,
    vessel, garment, weapon, the bronze serpent.

    A physical object that is also a text (the tablets of the covenant, the book of the law)
    is an Artifact. A living tree or plant is a Plant, not an Artifact.
    """


class Event(BaseModel):
    """A significant, datable occurrence or episode: creation, the flood, a covenant-making,
    an exodus, a battle, a miracle, a journey, a judgment.

    Prefer modeling who-did-what as RELATIONS between Person / Place / Group and reserve Event
    for the named happening itself. A blessing, curse, prophecy, or individual command is a
    relation (BLESSED, COMMANDED, PRONOUNCED_JUDGMENT_ON), not an Event. A recurring feast or
    holy day is a TimePeriod, not an Event.
    """


class TimePeriod(BaseModel):
    """A referenceable time marker or appointed time:

    a day ("the seventh day", the Sabbath), a feast or festival (Passover, Pentecost,
    Tabernacles, Unleavened Bread), an appointed observance (the Day of Atonement, the
    Jubilee), or an era ("the day of the LORD", "the last days"). This is the recurring day
    or feast itself; a one-time occurrence happening on it is an Event.
    """


class Title(BaseModel):
    """An office, rank, or role that a person holds — king, queen, prophet, priest, high
    priest, judge, apostle, scribe, Pharisee, Levite, Nazirite.

    This is the role ITSELF, not the person filling it: "David" is a Person who holds the
    Title "king". Use when a role is referenced as a category ("the kings of Judah", "the
    prophets"), so persons can connect to it via a HELD_TITLE relation.
    """


# Passed to Graphiti.add_episode(entity_types=...).
ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Person": Person,
    "Group": Group,
    "Place": Place,
    "Deity": Deity,
    "DivineBeing": DivineBeing,
    "Creature": Creature,
    "Plant": Plant,
    "Artifact": Artifact,
    "Event": Event,
    "TimePeriod": TimePeriod,
    "Title": Title,
}


# ---------------------------------------------------------------------------
# Edge (relationship) types
# ---------------------------------------------------------------------------
# Graphiti stores every edge as a Neo4j RELATES_TO relationship whose `name`
# property holds the predicate. Without a controlled vocabulary the LLM invents a
# fresh predicate per episode, so one relation surfaces under synonyms (SPOKE_TO
# vs SAID_TO, COMPASSES vs "goes toward"). Defining edge types pins the vocabulary:
# the *dict key* in EDGE_TYPES is the predicate the extractor is steered to reuse,
# and the class *docstring* is the semantic definition fed to the LLM — same
# convention, and same field-less rationale, as the entity types above.
#
# All edge types are advertised to the extractor on every episode; EDGE_TYPE_MAP
# declares which predicates are valid between which (source_label, target_label)
# node-type pairs. Graphiti always merges the ('Entity', 'Entity') entry into the
# candidate set for any pair, so predicates listed there are offered everywhere;
# the type-specific pairs add type-bound predicates on top. A predicate the LLM
# still emits outside this vocabulary is NOT dropped — it is kept as a generic
# RELATES_TO edge carrying that raw name.


class SpokeTo(BaseModel):
    """One entity verbally addresses another: says to, speaks to, calls to, asks, answers,
    replies. Use this single predicate for any act of speech directed at a hearer, regardless
    of the exact wording in the text."""


class Commanded(BaseModel):
    """One entity issues a binding command, charge, or instruction to another (a positive
    'you shall' / 'do this'). For a forbidding command use PROHIBITED instead."""


class Prohibited(BaseModel):
    """One entity forbids another from an action (a negative command, 'you shall not'). What
    is forbidden belongs in the fact text, not as a separate edge to the thing."""


class Blessed(BaseModel):
    """One entity pronounces a blessing or favor upon a person, group, creature, place, or
    appointed time."""


class Cursed(BaseModel):
    """One entity pronounces a curse, judgment, or sentence of punishment upon another."""


class Created(BaseModel):
    """A creative or generative act: to create, form, make, plant, or cause to grow an entity.
    Use for God forming man, planting a garden, or causing a tree to grow. For fashioning a
    physical object on someone's behalf use MADE_FOR instead."""


class MadeFor(BaseModel):
    """One entity fashions or provides a physical object for the benefit of another (e.g. God
    made coats of skins for Adam and Eve)."""


class Placed(BaseModel):
    """One entity sets or positions another entity at a location (e.g. God placed the cherubim
    and the flaming sword at the east of the garden)."""


class Gave(BaseModel):
    """One entity hands over or transfers an object to another (e.g. Eve gave the fruit to
    Adam)."""


class Named(BaseModel):
    """One entity assigns a name to another (e.g. Adam named his wife Eve)."""


class MarriedTo(BaseModel):
    """A marriage / spousal bond between two persons. Symmetric in meaning; record once."""


class ParentOf(BaseModel):
    """A parent-to-child kinship relation between two persons; direction is parent -> child.
    Covers 'father of', 'mother of', 'begat'."""


class HidFrom(BaseModel):
    """One entity conceals itself or hides from another (e.g. Adam hid from the presence of
    the LORD God)."""


class Deceived(BaseModel):
    """One entity deceives, tempts, or beguiles another into acting (e.g. the serpent deceived
    Eve)."""


class EnmityWith(BaseModel):
    """A relation of declared hostility, conflict, or enmity between two entities (e.g. the
    enmity put between the serpent and the woman). Symmetric in meaning; record once."""


class Exiled(BaseModel):
    """One entity drives out, sends forth, or banishes another from a place (e.g. God sent
    Adam forth from the Garden of Eden)."""


class LocatedIn(BaseModel):
    """A place is situated within, or part of, a larger place — a region, land, or
    territory."""


class FlowsThrough(BaseModel):
    """A river or watercourse runs through, around, toward, or waters a land or region. Use for
    any 'compasses', 'goes toward', or 'waters' relation between a river and the land it
    traverses."""


class Guards(BaseModel):
    """One entity protects, keeps, or blocks the way to another (e.g. the flaming sword guards
    the way to the Tree of Life)."""


class Consecrated(BaseModel):
    """One entity sets apart, sanctifies, hallows, or rests upon a time or place as holy (e.g.
    God sanctified and rested on the seventh day)."""


# Passed to Graphiti.add_episode(edge_types=...). Keys are the predicate names the
# extractor reuses; they are stored verbatim on the RELATES_TO edge's `name`.
EDGE_TYPES: dict[str, type[BaseModel]] = {
    "SPOKE_TO": SpokeTo,
    "COMMANDED": Commanded,
    "PROHIBITED": Prohibited,
    "BLESSED": Blessed,
    "CURSED": Cursed,
    "CREATED": Created,
    "MADE_FOR": MadeFor,
    "PLACED": Placed,
    "GAVE": Gave,
    "NAMED": Named,
    "MARRIED_TO": MarriedTo,
    "PARENT_OF": ParentOf,
    "HID_FROM": HidFrom,
    "DECEIVED": Deceived,
    "ENMITY_WITH": EnmityWith,
    "EXILED": Exiled,
    "LOCATED_IN": LocatedIn,
    "FLOWS_THROUGH": FlowsThrough,
    "GUARDS": Guards,
    "CONSECRATED": Consecrated,
}

# Passed to Graphiti.add_episode(edge_type_map=...). Keys are (source_label,
# target_label) pairs of ENTITY_TYPES names (or 'Entity'); values are the subset of
# EDGE_TYPES valid for that pair. ('Entity', 'Entity') is always merged in by
# Graphiti, so it holds the actor-agnostic predicates; the specific pairs add
# type-bound ones.
EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = {
    # Actor-agnostic predicates — offered for every (source, target) pair.
    ("Entity", "Entity"): [
        "SPOKE_TO", "COMMANDED", "PROHIBITED", "BLESSED", "CURSED",
        "GAVE", "NAMED", "HID_FROM", "DECEIVED", "ENMITY_WITH",
    ],
    # Divine agency: creation, provision, positioning, banishment, consecration.
    ("Deity", "Person"): ["CREATED", "MADE_FOR", "EXILED"],
    ("Deity", "Creature"): ["CREATED"],
    ("Deity", "Plant"): ["CREATED"],
    ("Deity", "Place"): ["CREATED", "PLACED"],
    ("Deity", "Artifact"): ["CREATED", "PLACED"],
    ("Deity", "DivineBeing"): ["CREATED", "PLACED"],
    ("Deity", "TimePeriod"): ["CONSECRATED"],
    # Kinship between persons.
    ("Person", "Person"): ["MARRIED_TO", "PARENT_OF"],
    # Geography: rivers and regions.
    ("Place", "Place"): ["LOCATED_IN", "FLOWS_THROUGH"],
    # Guarding the way to a place or plant.
    ("Artifact", "Plant"): ["GUARDS"],
    ("Artifact", "Place"): ["GUARDS"],
    ("DivineBeing", "Plant"): ["GUARDS"],
    ("DivineBeing", "Place"): ["GUARDS"],
}
