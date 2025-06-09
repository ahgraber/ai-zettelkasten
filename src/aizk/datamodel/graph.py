import asyncio
import base64
from collections import defaultdict
from typing import Any, Awaitable, Callable, Iterator, Literal, Optional

from datasketch import LeanMinHash, MinHash
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_serializer, model_validator
import xxhash

"""
Nodes should be constructed by providing (text, metadata). The rest of the fields should be filled by async functions that will be provided.

Nodes must use a unique ID, which is directly hashed from the text for easy exact deduplication.
Nodes should be immutable.
Nodes must be default serializable to/from JSON.
Use xxhash for fast hashing of text content to generate unique IDs (https://github.com/ifduyue/python-xxhash)
Use minhash for approximate similarity matching of text content (https://ekzhu.com/datasketch/documentation.html#lean-minhash)
"""


def _xxhash_wrapper(data: bytes, seed: int = 1) -> int:
    """Wrapper for xxhash that ensures compatibility with datasketch MinHash.

    Args:
        data: Input bytes to hash
        seed: Random seed for hashing

    Returns:
        Hash value as 64-bit unsigned integer (np.uint64 compatible)
    """
    hash_val = xxhash.xxh64(data, seed=seed).intdigest()
    # Ensure it's a positive 64-bit value (xxh64 returns signed, we need unsigned range)
    return hash_val & 0xFFFFFFFFFFFFFFFF


async def generate_minhash(text: str, **kwargs) -> list[int]:
    """Generate a MinHash signature for the given text.

    Args:
        text: The text content to generate MinHash for
        **kwargs: Additional parameters for MinHash (e.g., num_perm)

    Returns:
        List of ints (MinHash signature values) for similarity search
    """
    # Use our xxhash wrapper by default for consistency
    hashfunc = kwargs.pop("hashfunc", _xxhash_wrapper)
    seed = kwargs.pop("seed", 42)

    # Create MinHash with our custom hash function
    minhash = MinHash(hashfunc=hashfunc, seed=seed, **kwargs)

    tokens = text.split()
    for token in tokens:
        minhash.update(token.encode("utf8"))

    lean_minhash = LeanMinHash(minhash)
    return lean_minhash.hashvalues.tolist()


class Node(BaseModel):
    """A node in the knowledge graph representing a piece of content.

    Nodes are immutable after creation. If you need to change the text content,
    create a new node instead.

    Attributes:
        id: Unique identifier, auto-generated from text content using xxhash
        metadata: Additional metadata associated with the node
        text: The main text content of the node
        minhash: MinHash signature for approximate similarity matching
        embedding: Vector embedding for semantic similarity
        entities: Named entities extracted from the text
        tags: User-defined tags for categorization
    """

    model_config = ConfigDict(
        frozen=True,  # Make the model immutable
        arbitrary_types_allowed=True,  # Allow custom types
    )

    id: str = Field(default="", description="Unique identifier for the node")
    metadata: dict[str, str] = Field(default_factory=dict, description="Node metadata")
    text: str = Field(..., min_length=1, description="Text content of the node")
    minhash: list[int] = Field(default_factory=list, description="MinHash for jaccard similarity search")
    embedding: list[float] = Field(default_factory=list, description="Embedding vector")
    entities: set[str] = Field(default_factory=set, description="Extracted entities")
    tags: set[str] = Field(default_factory=set, description="User-defined tags")

    @field_serializer("entities", "tags")
    def serialize_sets(self, value: set[str]) -> list[str]:
        """Serialize sets to lists for JSON compatibility."""
        return list(value)

    @model_validator(mode="before")
    @classmethod
    def generate_id(cls, data: Any) -> Any:
        """Generate ID."""
        if isinstance(data, dict) and not data.get("id") and data.get("text"):
            data["id"] = xxhash.xxh128_hexdigest(data["text"])
        return data

    def __hash__(self) -> int:
        """Make nodes hashable based on their ID."""
        return hash(self.id)

    def __eq__(self, other) -> bool:
        """Node equality based on ID."""
        if not isinstance(other, Node):
            return False
        return self.id == other.id


class NodeProcessorError(Exception):
    """Custom exception for node processor failures."""

    pass


class NodeFactory:
    """Factory for creating fully-populated Node instances.

    Takes a mapping of node attributes to their processor functions.
    If a processor function is None, the node's default factory value is used.
    """

    DEFAULT_PROCESSORS: dict[str, Optional[Callable[[str], Awaitable[Any]]]] = {
        "minhash": generate_minhash,  # Use async minhash generation
        "embedding": None,  # Use default empty list
        "entities": None,  # Use default empty set
        "tags": None,  # Use default empty set
    }

    def __init__(self, processors: Optional[dict[str, Optional[Callable[[str], Awaitable[Any]]]]] = None):
        """Initialize the NodeFactory.

        Args:
            processors: Dict mapping node attribute names to async processor functions.
                        If a processor is None, the field's default factory is used.
                        If not provided, uses DEFAULT_PROCESSORS.
        """
        self.processors = processors if processors is not None else self.DEFAULT_PROCESSORS.copy()

    async def create_node(self, text: str, metadata: Optional[dict[str, str]] = None) -> Node:
        """Create a fully-populated Node instance.

        Args:
            text: The text content for the node
            metadata: Optional metadata dictionary

        Returns:
            A fully-populated, immutable Node instance
        """
        if metadata is None:
            metadata = {}

        # Start with basic node data
        node_data = {
            "text": text,
            "metadata": metadata,
        }

        # Run all processors concurrently
        async_tasks = []
        for field_name, processor in self.processors.items():
            if processor is not None:
                task = self._run_processor(processor, text, field_name)
                async_tasks.append(task)

        # Wait for all processors to complete
        if async_tasks:
            results = await asyncio.gather(*async_tasks, return_exceptions=True)

            # Process results
            for (field_name, _), result in zip([(k, v) for k, v in self.processors.items() if v is not None], results):
                if isinstance(result, Exception):
                    # Log error but continue with default value
                    print(f"Warning: Processor for {field_name} failed: {result}")
                else:
                    node_data[field_name] = result

        # Create and return the immutable node
        return Node(**node_data)

    async def _run_processor(self, processor: Callable[[str], Awaitable[Any]], text: str, field_name: str) -> Any:
        """Run a single processor function."""
        try:
            return await processor(text)
        except Exception as e:
            raise NodeProcessorError(f"Processor for {field_name} failed") from e


class Relationship(BaseModel):
    source_id: str = Field(..., description="ID of the source node")
    target_id: str = Field(..., description="ID of the target node")
    relationship_type: str = Field(..., description="Type of the relationship")

    def __hash__(self) -> int:
        """Make relationships hashable for deduplication."""
        return hash((self.source_id, self.target_id, self.relationship_type))

    def __eq__(self, other) -> bool:
        """Enable relationship equality comparison."""
        if not isinstance(other, Relationship):
            return False
        return (
            self.source_id == other.source_id
            and self.target_id == other.target_id
            and self.relationship_type == other.relationship_type
        )


"""
Graph:

- must have quick access to nodes by ID
- must be memory efficient
- must be serializable to/from json for saving
- should provide dunder methods
- operations should be idempotent

Further, I may want to add an "update" method that takes a list of  functions with signature '(a: node, b: node) -> bool' that determine whether a relationship between nodes a,b should be defined.
"""


class Graph(BaseModel):
    model_config = ConfigDict()

    nodes: dict[str, Node] = Field(default_factory=dict, description="Dictionary of nodes indexed by their IDs")
    relationships: set[Relationship] = Field(
        default_factory=set, description="Set of unique relationships between nodes"
    )

    @field_serializer("relationships")
    def serialize_relationships(self, value: set[Relationship]) -> list[dict]:
        """Serialize relationships set to list for JSON compatibility."""
        return [rel.model_dump() for rel in value]

    # Private attribute for relationship index (not included in serialization)
    _relationship_index: defaultdict[str, set[Relationship]] = PrivateAttr(default_factory=lambda: defaultdict(set))

    def model_post_init(self, __context) -> None:
        """Initialize the relationship index after model creation."""
        self._relationship_index = defaultdict(set)
        self._rebuild_relationship_index()

    def _rebuild_relationship_index(self) -> None:
        """Rebuild the relationship index for faster lookups."""
        self._relationship_index = defaultdict(set)
        for rel in self.relationships:
            self._relationship_index[rel.source_id].add(rel)
            self._relationship_index[rel.target_id].add(rel)

    def add_node(self, node: Node) -> bool:
        """Add a node to the graph (idempotent).

        Args:
            node: The node to add to the graph.

        Returns:
            bool: True if node was added, False if it already existed.
        """
        if node.id in self.nodes:
            return False
        self.nodes[node.id] = node
        return True

    def remove_node(self, node_id: str) -> bool:
        """Remove a node and all its relationships from the graph.

        Args:
            node_id: ID of the node to remove.

        Returns:
            bool: True if node was removed, False if it didn't exist.
        """
        if node_id not in self.nodes:
            return False

        # Remove all relationships involving this node
        relationships_to_remove = set(self._relationship_index.get(node_id, set()))
        for rel in relationships_to_remove:
            self.relationships.discard(rel)

        # Remove node
        del self.nodes[node_id]

        # Rebuild index (could be optimized)
        self._rebuild_relationship_index()
        return True

    def add_relationship(self, relationship: Relationship) -> bool:
        """Add a relationship to the graph (idempotent).

        Args:
            relationship: The relationship to add.

        Returns:
            bool: True if relationship was added, False if it already existed.

        Raises:
            ValueError: If either source or target node doesn't exist.
        """
        if relationship in self.relationships:
            return False

        # Validate that both nodes exist
        if (relationship.source_id not in self.nodes) or (relationship.target_id not in self.nodes):
            raise ValueError(
                f"Cannot add relationship: nodes {relationship.source_id} or {relationship.target_id} do not exist"
            )

        self.relationships.add(relationship)

        # Update index
        self._relationship_index[relationship.source_id].add(relationship)
        self._relationship_index[relationship.target_id].add(relationship)

        return True

    def get_node(self, node_id: str) -> Node | None:
        """Get a node by its ID.

        Args:
            node_id: The ID of the node to retrieve.

        Returns:
            The node if found, None otherwise.
        """
        return self.nodes.get(node_id)

    def get_relationships(self, node_id: str) -> set[Relationship]:
        """Get all relationships for a given node ID.

        Args:
            node_id: The ID of the node to get relationships for.

        Returns:
            Set of relationships involving the node.
        """
        return set(self._relationship_index[node_id])

    def get_relationships_to(self, node_id: str) -> set[Relationship]:
        """Get relationships where node is the source.

        Args:
            node_id: The ID of the node to get relationships for.

        Returns:
            Set of relationships where node is the source.
        """
        return {rel for rel in self.get_relationships(node_id) if rel.source_id == node_id}

    def get_relationships_from(self, node_id: str) -> set[Relationship]:
        """Get relationships where node is the target.

        Args:
            node_id: The ID of the node to get relationships for.

        Returns:
            Set of relationships involving the node.
        """
        return {rel for rel in self.get_relationships(node_id) if rel.target_id == node_id}

    def get_neighbors(self, node_id: str, distance: int = 1) -> set[Node]:
        """Get all neighbors for a given node ID within specified distance.

        Args:
            node_id: The ID of the node to get neighbors for.
            distance: The maximum distance to traverse in the graph (default is 1).
                      Distance 1 returns direct neighbors only.

        Returns:
            Set of neighboring nodes within the specified distance.

        Raises:
            ValueError: If distance is less than 1 or node_id doesn't exist.
        """
        if distance < 1:
            raise ValueError("Distance must be at least 1")

        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} does not exist in graph")

        visited = {node_id}  # Start with the original node
        current_level = {node_id}

        for _ in range(distance):
            next_level = set()
            for current_node_id in current_level:
                # Get direct neighbors of current node
                for rel in self.get_relationships(current_node_id):
                    neighbor_id = rel.target_id if rel.source_id == current_node_id else rel.source_id
                    if neighbor_id not in visited:
                        next_level.add(neighbor_id)
                        visited.add(neighbor_id)

            current_level = next_level
            if not current_level:  # No more neighbors to explore
                break

        # Remove the original node from results and return Node objects
        visited.discard(node_id)
        return {self.nodes[neighbor_id] for neighbor_id in visited if neighbor_id in self.nodes}

    def get_similar(
        self, target_node: Node, relationship: str, direction: Literal["<", "<=", ">=", ">"], threshold: float = 0.7
    ) -> list[tuple[Node, float]]:
        """Get nodes similar to the target node based on relationship type."""
        ...

    def update_relationships(self, relationship_functions: list[Callable[[Node, Node], bool]]) -> int:
        """Update relationships using provided functions.

        Args:
            relationship_functions: List of functions that take two nodes and return
                                  True if a relationship should exist between them.

        Returns:
            int: Number of new relationships added.
        """
        new_relationships = 0
        node_list = list(self.nodes.values())

        for i, node_a in enumerate(node_list):
            for node_b in node_list[i + 1 :]:  # Avoid duplicate pairs
                for rel_func in relationship_functions:
                    if rel_func(node_a, node_b):
                        # Determine relationship type based on function name
                        rel_type = getattr(rel_func, "__name__", "related")
                        relationship = Relationship(
                            source_id=node_a.id, target_id=node_b.id, relationship_type=rel_type
                        )
                        if self.add_relationship(relationship):
                            new_relationships += 1

        return new_relationships

    def to_dict(self) -> dict:
        """Convert graph to dictionary for JSON serialization."""
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict) -> "Graph":
        """Create graph from dictionary (for JSON deserialization)."""
        graph = cls()

        # Add nodes first
        for node_data in data.get("nodes", {}).values():
            node = Node(**node_data)
            graph.add_node(node)

        # Then add relationships
        for rel_data in data.get("relationships", []):
            relationship = Relationship(**rel_data)
            try:
                graph.add_relationship(relationship)
            except ValueError:
                # Skip relationships with missing nodes
                continue

        return graph

    def __len__(self) -> int:
        """Return the number of nodes in the graph."""
        return len(self.nodes)

    def __iter__(self) -> Iterator[Node]:  # type: ignore[override]
        """Iterate over the nodes in the graph."""
        return iter(self.nodes.values())

    def __contains__(self, node_id: str) -> bool:
        """Check if a node exists in the graph by its ID."""
        return node_id in self.nodes

    def __repr__(self) -> str:
        """Return a string representation of the graph."""
        return f"Graph(nodes={len(self.nodes)}, relationships={len(self.relationships)})"
