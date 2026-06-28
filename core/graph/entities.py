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
  * Kinship / genealogy ("son of", "father of") -> relation (FATHER_OF, DESCENDANT_OF).
  * Numbers / quantities ("forty days", "twelve tribes") -> attribute of a fact.
  * Divine names / attributes ("the Almighty", "Emmanuel") -> aliases of the Deity node.
  * Prophecies / blessings / curses / individual laws -> relations with attributes
    (BLESSED, COMMANDED, PRONOUNCED_JUDGMENT_ON), not Event nodes.

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


class Artifact(BaseModel):
    """A notable physical object: ark, tabernacle furnishing, altar, idol, scroll, tablets,
    vessel, garment, weapon, the bronze serpent.

    A physical object that is also a text (the tablets of the covenant, the book of the law)
    is an Artifact.
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
    "Artifact": Artifact,
    "Event": Event,
    "TimePeriod": TimePeriod,
    "Title": Title,
}
