"""Comprehensive unit tests for the graph module.

This module tests all components of the knowledge graph including:
- Helper functions for minhash serialization
- Node creation, validation, and behavior
- NodeFactory for async node creation
- Relationship modeling
- Graph operations and integrity
- Custom exceptions
"""

import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch

from pydantic import ValidationError
import pytest
import xxhash

from aizk.datamodel.graph import (
    Graph,
    Node,
    NodeFactory,
    NodeProcessorError,
    Relationship,
)


class TestNode:
    """Test Node creation, validation, and behavior."""

    def test_node_creation_minimal(self):
        """Test creating a node with minimal required data."""
        # Arrange & Act
        node = Node(text="Test content")

        # Assert
        assert node.text == "Test content"
        assert node.id != ""  # ID should be auto-generated
        assert node.metadata == {}
        assert node.embedding == []
        assert node.entities == set()
        assert node.tags == set()

    def test_node_creation_with_all_fields(self):
        """Test creating a node with all fields specified."""
        # Arrange
        metadata = {"source": "test", "timestamp": "2025-01-01"}
        embedding = [0.1, 0.2, 0.3]
        entities = {"entity1", "entity2"}
        tags = {"tag1", "tag2"}

        # Act
        node = Node(text="Test content", metadata=metadata, embedding=embedding, entities=entities, tags=tags)

        # Assert
        assert node.text == "Test content"
        assert node.metadata == metadata
        assert node.embedding == embedding
        assert node.entities == entities
        assert node.tags == tags

    def test_node_id_generation_deterministic(self):
        """Test that nodes with same text get same ID."""
        # Arrange
        text = "Identical content"

        # Act
        node1 = Node(text=text)
        node2 = Node(text=text)

        # Assert
        assert node1.id == node2.id
        assert node1.id == xxhash.xxh128_hexdigest(text)

    @pytest.mark.parametrize(
        "text1,text2,should_be_equal",
        [
            # Very different content
            ("Content one", "Content two", False),
            ("Hello world", "Goodbye universe", False),
            ("Short", "This is a much longer piece of text with many words", False),
            ("English text", "中文内容", False),
            # Extremely similar cases - minute differences
            ("Content", "Content ", False),  # Trailing space
            (" Content", "Content", False),  # Leading space
            ("Content", "Content\n", False),  # Different line ending
            ("Content\n", "Content\r\n", False),  # Unix vs Windows line endings
            ("Content\r\n", "Content\r", False),  # Windows vs old Mac line endings
            ("Content\t", "Content ", False),  # Tab vs space
            ("Content  ", "Content ", False),  # Double space vs single space
            # Unicode lookalikes (should be different)
            ("Content", "Сontent", False),  # Latin 'C' vs Cyrillic 'С'
            ("cafe", "café", False),  # ASCII vs accented character
            ("А", "A", False),  # Cyrillic 'А' vs Latin 'A'
            ("0", "О", False),  # Digit '0' vs Cyrillic 'О'
            ("H", "Н", False),  # Latin 'H' vs Cyrillic 'Н'
            # Zero-width characters and invisible differences
            ("test", "te\u200bst", False),  # Zero-width space
            ("test", "test\u200c", False),  # Zero-width non-joiner
            ("test", "test\ufeff", False),  # Zero-width no-break space (BOM)
            # Normalization differences (NFC vs NFD)
            ("café", "cafe\u0301", False),  # Precomposed vs decomposed
            ("naïve", "nai\u0308ve", False),  # Different Unicode normalization
            # Identical content (should be equal)
            ("Identical content", "Identical content", True),
            ("Multi\nline\ntext", "Multi\nline\ntext", True),
            ("Unicode: 你好世界", "Unicode: 你好世界", True),
        ],
    )
    def test_node_id_generation_text_variations(self, text1: str, text2: str, should_be_equal: bool):
        """Test node ID generation with various text similarities and differences.

        This test covers:
        - Very different content
        - Minute differences (spaces, line endings)
        - Unicode lookalikes that appear identical but aren't
        - Zero-width and invisible characters
        - Unicode normalization differences
        - Truly identical content
        """
        # Arrange & Act
        node1 = Node(text=text1)
        node2 = Node(text=text2)

        # Assert
        if should_be_equal:
            assert node1.id == node2.id, f"Expected identical IDs for: {repr(text1)} vs {repr(text2)}"
        else:
            assert node1.id != node2.id, f"Expected different IDs for: {repr(text1)} vs {repr(text2)}"

    def test_node_custom_id_preserved(self):
        """Test that explicitly provided ID is preserved."""
        # Arrange
        custom_id = "custom-node-id"

        # Act
        node = Node(text="Test content", id=custom_id)

        # Assert
        assert node.id == custom_id

    def test_node_immutability(self):
        """Test that nodes are immutable after creation."""
        # Arrange
        node = Node(text="Test content")

        # Act & Assert
        with pytest.raises(ValidationError):
            node.text = "New content"

    def test_node_empty_text_validation(self):
        """Test that empty text raises validation error with appropriate message."""
        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            Node(text="")

        error_str = str(exc_info.value)
        assert "at least 1 character" in error_str

    def test_node_none_text_validation(self):
        """Test that None text raises validation error with appropriate message."""
        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            Node(text=None)  # type: ignore[arg-type]

        error_str = str(exc_info.value)
        # Check for common validation error patterns for None/required fields
        assert any(
            keyword in error_str.lower()
            for keyword in ["none", "null", "required", "missing", "input should be a valid string"]
        )

    @pytest.mark.parametrize(
        "invalid_text",
        [
            "",
            None,
        ],
    )
    def test_node_invalid_text_values(self, invalid_text):
        """Test that various invalid text values raise ValidationError."""
        # Act & Assert
        with pytest.raises(ValidationError):
            Node(text=invalid_text)  # type: ignore[arg-type]

    def test_node_equality_by_id(self):
        """Test that nodes are equal if they have the same ID."""
        # Arrange
        node1 = Node(text="Content", id="same-id")
        node2 = Node(text="Different content", id="same-id")
        node3 = Node(text="Content", id="different-id")

        # Act & Assert
        assert node1 == node2  # Same ID
        assert node1 != node3  # Different ID
        assert node1 != "not a node"  # Different type

    def test_node_hashable(self):
        """Test that nodes can be used in sets and as dict keys."""
        # Arrange
        node1 = Node(text="Content 1")
        node2 = Node(text="Content 2")
        node3 = Node(text="Content 1")  # Same as node1

        # Act
        node_set = {node1, node2, node3}

        # Assert
        assert len(node_set) == 2  # node1 and node3 are the same
        assert hash(node1) == hash(node3)
        assert hash(node1) != hash(node2)

    def test_node_json_serialization(self):
        """Test that nodes can be serialized to JSON."""
        # Arrange
        node = Node(
            text="Test content",
            metadata={"key": "value"},
            entities={"entity1", "entity2"},
            tags={"tag1"},
        )

        # Act
        json_data = node.model_dump_json()
        parsed = json.loads(json_data)

        # Assert
        assert parsed["text"] == "Test content"
        assert parsed["metadata"] == {"key": "value"}
        assert set(parsed["entities"]) == {"entity1", "entity2"}
        assert set(parsed["tags"]) == {"tag1"}


# class TestGenerateMinhash:
#     """Test the generate_minhash async function."""

#     @pytest.mark.asyncio
#     async def test_generate_minhash_basic(self):
#         """Test basic minhash generation returns list of uint64 values."""
#         # Arrange
#         text = "This is a test document with some words"

#         # Act
#         result = await generate_minhash(text)

#         # Assert
#         assert isinstance(result, list)
#         assert len(result) > 0  # Should have minhash values
#         assert all(isinstance(val, int) for val in result)
#         # Check that values are in uint64 range (0 to 2^64-1)
#         assert all(0 <= val <= 0xFFFFFFFFFFFFFFFF for val in result)

#     @pytest.mark.asyncio
#     async def test_generate_minhash_deterministic(self):
#         """Test that same text produces same minhash list."""
#         # Arrange
#         text = "Consistent test content"

#         # Act
#         result1 = await generate_minhash(text)
#         result2 = await generate_minhash(text)

#         # Assert
#         assert result1 == result2  # Same text should produce identical lists

#     @pytest.mark.asyncio
#     async def test_generate_minhash_different_text(self):
#         """Test that different text produces different minhash lists."""
#         # Arrange
#         text1 = "First document content"
#         text2 = "Second document content"

#         # Act
#         result1 = await generate_minhash(text1)
#         result2 = await generate_minhash(text2)

#         # Assert
#         assert result1 != result2  # Different text should produce different lists

#     @pytest.mark.asyncio
#     async def test_generate_minhash_with_kwargs(self):
#         """Test minhash generation with custom parameters."""
#         # Arrange
#         text = "Test content"

#         # Act
#         result = await generate_minhash(text, num_perm=64)

#         # Assert
#         assert isinstance(result, list)
#         assert len(result) == 64  # Should have num_perm values
#         assert all(isinstance(val, int) for val in result)
#         assert all(0 <= val <= 0xFFFFFFFFFFFFFFFF for val in result)

#     @pytest.mark.asyncio
#     async def test_generate_minhash_empty_text(self):
#         """Test minhash generation with empty text."""
#         # Arrange
#         text = ""

#         # Act
#         result = await generate_minhash(text)

#         # Assert
#         assert isinstance(result, list)
#         assert len(result) > 0  # Should still generate minhash values
#         assert all(isinstance(val, int) for val in result)

#     @pytest.mark.asyncio
#     async def test_generate_minhash_single_token(self):
#         """Test minhash generation with single token."""
#         # Arrange
#         text = "singleword"

#         # Act
#         result = await generate_minhash(text)

#         # Assert
#         assert isinstance(result, list)
#         assert len(result) > 0
#         assert all(isinstance(val, int) for val in result)

#     @pytest.mark.asyncio
#     async def test_generate_minhash_custom_seed(self):
#         """Test minhash generation with custom seed produces different results."""
#         # Arrange
#         text = "Test content for seed comparison"

#         # Act
#         result1 = await generate_minhash(text, seed=42)
#         result2 = await generate_minhash(text, seed=123)

#         # Assert
#         assert isinstance(result1, list)
#         assert isinstance(result2, list)
#         assert len(result1) == len(result2)
#         assert result1 != result2  # Different seeds should produce different results


class TestNodeFactory:
    """Test NodeFactory for async node creation."""

    def test_node_factory_initialization_default(self):
        """Test NodeFactory initialization with default processors."""
        # Act
        factory = NodeFactory()

        # Assert
        assert factory.processors == NodeFactory.DEFAULT_PROCESSORS

    def test_node_factory_initialization_custom(self):
        """Test NodeFactory initialization with custom processors."""
        # Arrange
        custom_processors = {"embedding": AsyncMock(), "entities": None}

        # Act
        factory = NodeFactory(custom_processors)

        # Assert
        assert factory.processors == custom_processors

    @pytest.mark.asyncio
    async def test_create_node_minimal(self):
        """Test creating a node with minimal input."""
        # Arrange
        factory = NodeFactory({"minhash": None})  # Disable async processors

        # Act
        node = await factory.create_node("Test content")

        # Assert
        assert isinstance(node, Node)
        assert node.text == "Test content"
        assert node.metadata == {}

    @pytest.mark.asyncio
    async def test_create_node_with_metadata(self):
        """Test creating a node with metadata."""
        # Arrange
        factory = NodeFactory({"minhash": None})
        metadata = {"source": "test", "category": "example"}

        # Act
        node = await factory.create_node("Test content", metadata)

        # Assert
        assert node.metadata == metadata

    @pytest.mark.asyncio
    async def test_create_node_with_processors(self):
        """Test creating a node with async processors."""
        # Arrange
        mock_embedding_processor = AsyncMock(return_value=[0.1, 0.2, 0.3])
        mock_entities_processor = AsyncMock(return_value={"entity1", "entity2"})

        processors = {
            "embedding": mock_embedding_processor,
            "entities": mock_entities_processor,
            "minhash": None,
            "tags": None,
        }
        factory = NodeFactory(processors)

        # Act
        node = await factory.create_node("Test content")

        # Assert
        assert node.embedding == [0.1, 0.2, 0.3]
        assert node.entities == {"entity1", "entity2"}
        mock_embedding_processor.assert_called_once_with("Test content")
        mock_entities_processor.assert_called_once_with("Test content")

    @pytest.mark.asyncio
    async def test_create_node_processor_failure(self):
        """Test node creation when a processor fails."""
        # Arrange
        failing_processor = AsyncMock(side_effect=Exception("Processor failed"))
        processors = {"embedding": failing_processor, "minhash": None}
        factory = NodeFactory(processors)

        # Act
        with patch("builtins.print") as mock_print:
            node = await factory.create_node("Test content")

        # Assert
        assert isinstance(node, Node)
        assert node.embedding == []  # Should use default value
        mock_print.assert_called_once()
        assert "Warning: Processor for embedding failed" in mock_print.call_args[0][0]

    @pytest.mark.asyncio
    async def test_run_processor_success(self):
        """Test successful processor execution."""
        # Arrange
        factory = NodeFactory()
        mock_processor = AsyncMock(return_value="processed_result")

        # Act
        result = await factory._run_processor(mock_processor, "test text", "test_field")

        # Assert
        assert result == "processed_result"
        mock_processor.assert_called_once_with("test text")

    @pytest.mark.asyncio
    async def test_run_processor_failure(self):
        """Test processor failure handling."""
        # Arrange
        factory = NodeFactory()
        mock_processor = AsyncMock(side_effect=ValueError("Test error"))

        # Act & Assert
        with pytest.raises(NodeProcessorError) as exc_info:
            await factory._run_processor(mock_processor, "test text", "test_field")

        assert "Processor for test_field failed" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, ValueError)


class TestRelationship:
    """Test Relationship model functionality."""

    def test_relationship_creation_valid(self):
        """Test creating a valid relationship."""
        # Act
        rel = Relationship(source_id="node1", target_id="node2", relationship_type="related_to")

        # Assert
        assert rel.source_id == "node1"
        assert rel.target_id == "node2"
        assert rel.relationship_type == "related_to"

    def test_relationship_equality(self):
        """Test relationship equality comparison."""
        # Arrange
        rel1 = Relationship(source_id="A", target_id="B", relationship_type="type1")
        rel2 = Relationship(source_id="A", target_id="B", relationship_type="type1")
        rel3 = Relationship(source_id="A", target_id="B", relationship_type="type2")
        rel4 = Relationship(source_id="B", target_id="A", relationship_type="type1")

        # Act & Assert
        assert rel1 == rel2  # Same content
        assert rel1 != rel3  # Different type
        assert rel1 != rel4  # Different direction
        assert rel1 != "not a relationship"  # Different type

    def test_relationship_hashable(self):
        """Test that relationships can be used in sets."""
        # Arrange
        rel1 = Relationship(source_id="A", target_id="B", relationship_type="type1")
        rel2 = Relationship(source_id="A", target_id="B", relationship_type="type1")
        rel3 = Relationship(source_id="A", target_id="C", relationship_type="type1")

        # Act
        rel_set = {rel1, rel2, rel3}

        # Assert
        assert len(rel_set) == 2  # rel1 and rel2 are duplicates
        assert hash(rel1) == hash(rel2)
        assert hash(rel1) != hash(rel3)

    def test_relationship_required_fields(self):
        """Test that all fields are required."""
        # Act & Assert
        with pytest.raises(ValidationError):
            Relationship(source_id="A", target_id="B")  # Missing relationship_type

        with pytest.raises(ValidationError):
            Relationship(source_id="A", relationship_type="type")  # Missing target_id

        with pytest.raises(ValidationError):
            Relationship(target_id="B", relationship_type="type")  # Missing source_id


class TestGraph:
    """Test Graph model and operations."""

    def test_graph_creation_empty(self):
        """Test creating an empty graph."""
        # Act
        graph = Graph()

        # Assert
        assert len(graph) == 0
        assert len(graph.nodes) == 0
        assert len(graph.relationships) == 0

    def test_add_node_success(self):
        """Test successfully adding a node to the graph."""
        # Arrange
        graph = Graph()
        node = Node(text="Test node")

        # Act
        result = graph.add_node(node)

        # Assert
        assert result is node
        assert len(graph) == 1
        assert node.id in graph.nodes
        assert graph.nodes[node.id] == node

    def test_add_node_duplicate(self):
        """Test adding the same node twice (idempotent operation)."""
        # Arrange
        graph = Graph()
        node = Node(text="Test node")

        # Act
        result1 = graph.add_node(node)
        result2 = graph.add_node(node)

        # Assert
        assert result1 is node
        assert result2 is node  # should return the same node
        assert len(graph) == 1

    def test_remove_node_success(self):
        """Test successfully removing a node."""
        # Arrange
        graph = Graph()
        node = Node(text="Test node")
        graph.add_node(node)

        # Act
        result = graph.remove_node(node.id)

        # Assert
        assert result is True
        assert len(graph) == 0
        assert node.id not in graph.nodes

    def test_remove_node_nonexistent(self):
        """Test removing a non-existent node."""
        # Arrange
        graph = Graph()

        # Act
        result = graph.remove_node("nonexistent-id")

        # Assert
        assert result is False

    def test_remove_node_with_relationships(self):
        """Test that removing a node also removes its relationships."""
        # Arrange
        graph = Graph()
        node1 = Node(text="Node 1")
        node2 = Node(text="Node 2")
        node3 = Node(text="Node 3")

        graph.add_node(node1)
        graph.add_node(node2)
        graph.add_node(node3)

        rel1 = Relationship(source_id=node1.id, target_id=node2.id, relationship_type="related")
        rel2 = Relationship(source_id=node2.id, target_id=node3.id, relationship_type="related")

        graph.add_relationship(rel1)
        graph.add_relationship(rel2)

        # Act
        graph.remove_node(node2.id)

        # Assert
        assert len(graph.relationships) == 0  # Both relationships should be removed
        assert len(graph.nodes) == 2

    def test_add_relationship_success(self):
        """Test successfully adding a relationship."""
        # Arrange
        graph = Graph()
        node1 = Node(text="Node 1")
        node2 = Node(text="Node 2")
        graph.add_node(node1)
        graph.add_node(node2)

        rel = Relationship(source_id=node1.id, target_id=node2.id, relationship_type="related")

        # Act
        result = graph.add_relationship(rel)

        # Assert
        assert result is True
        assert len(graph.relationships) == 1
        assert rel in graph.relationships

    def test_add_relationship_duplicate(self):
        """Test adding the same relationship twice (idempotent)."""
        # Arrange
        graph = Graph()
        node1 = Node(text="Node 1")
        node2 = Node(text="Node 2")
        graph.add_node(node1)
        graph.add_node(node2)

        rel = Relationship(source_id=node1.id, target_id=node2.id, relationship_type="related")

        # Act
        result1 = graph.add_relationship(rel)
        result2 = graph.add_relationship(rel)

        # Assert
        assert result1 is True
        assert result2 is False
        assert len(graph.relationships) == 1

    def test_add_relationship_missing_source_node(self):
        """Test adding relationship with missing source node."""
        # Arrange
        graph = Graph()
        node2 = Node(text="Node 2")
        graph.add_node(node2)

        rel = Relationship(source_id="nonexistent", target_id=node2.id, relationship_type="related")

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            graph.add_relationship(rel)

        assert "do not exist" in str(exc_info.value)

    def test_add_relationship_missing_target_node(self):
        """Test adding relationship with missing target node."""
        # Arrange
        graph = Graph()
        node1 = Node(text="Node 1")
        graph.add_node(node1)

        rel = Relationship(source_id=node1.id, target_id="nonexistent", relationship_type="related")

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            graph.add_relationship(rel)

        assert "do not exist" in str(exc_info.value)

    def test_get_node_existing(self):
        """Test retrieving an existing node."""
        # Arrange
        graph = Graph()
        node = Node(text="Test node")
        graph.add_node(node)

        # Act
        result = graph.get_node(node.id)

        # Assert
        assert result == node

    def test_get_node_nonexistent(self):
        """Test retrieving a non-existent node."""
        # Arrange
        graph = Graph()

        # Act
        result = graph.get_node("nonexistent")

        # Assert
        assert result is None

    def test_get_relationships_for_node(self):
        """Test getting all relationships for a specific node."""
        # Arrange
        graph = Graph()
        node1 = Node(text="Node 1")
        node2 = Node(text="Node 2")
        node3 = Node(text="Node 3")

        graph.add_node(node1)
        graph.add_node(node2)
        graph.add_node(node3)

        rel1 = Relationship(source_id=node1.id, target_id=node2.id, relationship_type="related")
        rel2 = Relationship(source_id=node3.id, target_id=node1.id, relationship_type="similar")
        rel3 = Relationship(source_id=node2.id, target_id=node3.id, relationship_type="related")

        graph.add_relationship(rel1)
        graph.add_relationship(rel2)
        graph.add_relationship(rel3)

        # Act
        relationships = graph.get_relationships(node1.id)

        # Assert
        assert len(relationships) == 2
        assert rel1 in relationships
        assert rel2 in relationships
        assert rel3 not in relationships

    def test_get_relationships_to_node(self):
        """Test getting relationships where node is the source."""
        # Arrange
        graph = Graph()
        node1 = Node(text="Node 1")
        node2 = Node(text="Node 2")
        node3 = Node(text="Node 3")

        graph.add_node(node1)
        graph.add_node(node2)
        graph.add_node(node3)

        rel1 = Relationship(source_id=node1.id, target_id=node2.id, relationship_type="related")
        rel2 = Relationship(source_id=node3.id, target_id=node1.id, relationship_type="similar")

        graph.add_relationship(rel1)
        graph.add_relationship(rel2)

        # Act
        relationships = graph.get_relationships_to(node1.id)

        # Assert
        assert len(relationships) == 1
        assert rel1 in relationships
        assert rel2 not in relationships

    def test_get_relationships_from_node(self):
        """Test getting relationships where node is the target."""
        # Arrange
        graph = Graph()
        node1 = Node(text="Node 1")
        node2 = Node(text="Node 2")
        node3 = Node(text="Node 3")

        graph.add_node(node1)
        graph.add_node(node2)
        graph.add_node(node3)

        rel1 = Relationship(source_id=node1.id, target_id=node2.id, relationship_type="related")
        rel2 = Relationship(source_id=node3.id, target_id=node1.id, relationship_type="similar")

        graph.add_relationship(rel1)
        graph.add_relationship(rel2)

        # Act
        relationships = graph.get_relationships_from(node1.id)

        # Assert
        assert len(relationships) == 1
        assert rel2 in relationships
        assert rel1 not in relationships

    def test_get_neighbors_direct(self):
        """Test getting direct neighbors (distance=1)."""
        # Arrange
        graph = Graph()
        node1 = Node(text="Node 1")
        node2 = Node(text="Node 2")
        node3 = Node(text="Node 3")
        node4 = Node(text="Node 4")

        for node in [node1, node2, node3, node4]:
            graph.add_node(node)

        # Create relationships: 1 -> 2 -> 3, 1 -> 4
        graph.add_relationship(Relationship(source_id=node1.id, target_id=node2.id, relationship_type="related"))
        graph.add_relationship(Relationship(source_id=node2.id, target_id=node3.id, relationship_type="related"))
        graph.add_relationship(Relationship(source_id=node1.id, target_id=node4.id, relationship_type="related"))

        # Act
        neighbors = graph.get_neighbors(node1.id, distance=1)

        # Assert
        assert len(neighbors) == 2
        neighbor_ids = {n.id for n in neighbors}
        assert node2.id in neighbor_ids
        assert node4.id in neighbor_ids
        assert node3.id not in neighbor_ids

    def test_get_neighbors_distance_two(self):
        """Test getting neighbors within distance=2."""
        # Arrange
        graph = Graph()
        node1 = Node(text="Node 1")
        node2 = Node(text="Node 2")
        node3 = Node(text="Node 3")
        node4 = Node(text="Node 4")

        for node in [node1, node2, node3, node4]:
            graph.add_node(node)

        # Create chain: 1 -> 2 -> 3 -> 4
        graph.add_relationship(Relationship(source_id=node1.id, target_id=node2.id, relationship_type="related"))
        graph.add_relationship(Relationship(source_id=node2.id, target_id=node3.id, relationship_type="related"))
        graph.add_relationship(Relationship(source_id=node3.id, target_id=node4.id, relationship_type="related"))

        # Act
        neighbors = graph.get_neighbors(node1.id, distance=2)

        # Assert
        assert len(neighbors) == 2
        neighbor_ids = {n.id for n in neighbors}
        assert node2.id in neighbor_ids
        assert node3.id in neighbor_ids
        assert node4.id not in neighbor_ids

    def test_get_neighbors_invalid_distance(self):
        """Test get_neighbors with invalid distance."""
        # Arrange
        graph = Graph()
        node = Node(text="Test node")
        graph.add_node(node)

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            graph.get_neighbors(node.id, distance=0)

        assert "Distance must be at least 1" in str(exc_info.value)

    def test_get_neighbors_nonexistent_node(self):
        """Test get_neighbors with non-existent node."""
        # Arrange
        graph = Graph()

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            graph.get_neighbors("nonexistent")

        assert "does not exist in graph" in str(exc_info.value)

    def test_update_relationships(self):
        """Test updating relationships using relationship functions."""
        # Arrange
        graph = Graph()
        node1 = Node(text="Node 1", tags={"tag1"})
        node2 = Node(text="Node 2", tags={"tag1"})
        node3 = Node(text="Node 3", tags={"tag2"})

        for node in [node1, node2, node3]:
            graph.add_node(node)

        def same_tag_relationship(a: Node, b: Node) -> bool:
            """Return True if nodes share at least one tag."""
            return bool(a.tags & b.tags)

        # Act
        new_relationships = graph.update_relationships([same_tag_relationship])

        # Assert
        assert new_relationships == 1  # Only node1 and node2 share tags
        relationships = list(graph.relationships)
        assert len(relationships) == 1
        rel = relationships[0]
        assert rel.relationship_type == "same_tag_relationship"
        assert {rel.source_id, rel.target_id} == {node1.id, node2.id}

    def test_to_dict_serialization(self):
        """Test converting graph to dictionary."""
        # Arrange
        graph = Graph()
        node1 = Node(text="Node 1")
        node2 = Node(text="Node 2")
        graph.add_node(node1)
        graph.add_node(node2)

        rel = Relationship(source_id=node1.id, target_id=node2.id, relationship_type="related")
        graph.add_relationship(rel)

        # Act
        graph_dict = graph.to_dict()

        # Assert
        assert "nodes" in graph_dict
        assert "relationships" in graph_dict
        assert len(graph_dict["nodes"]) == 2
        assert len(graph_dict["relationships"]) == 1

        # Verify JSON serializable
        json.dumps(graph_dict)

    def test_from_dict_deserialization(self):
        """Test creating graph from dictionary."""
        # Arrange
        graph_data = {
            "nodes": {
                "node1": {
                    "text": "Node 1",
                    "id": "node1",
                    "metadata": {},
                    "embedding": [],
                    "entities": [],
                    "tags": [],
                },
                "node2": {
                    "text": "Node 2",
                    "id": "node2",
                    "metadata": {},
                    "embedding": [],
                    "entities": [],
                    "tags": [],
                },
            },
            "relationships": [{"source_id": "node1", "target_id": "node2", "relationship_type": "related"}],
        }

        # Act
        graph = Graph.from_dict(graph_data)

        # Assert
        assert len(graph) == 2
        assert len(graph.relationships) == 1
        assert "node1" in graph.nodes
        assert "node2" in graph.nodes

    def test_from_dict_missing_nodes_in_relationships(self):
        """Test from_dict skips relationships with missing nodes."""
        # Arrange
        graph_data = {
            "nodes": {
                "node1": {
                    "text": "Node 1",
                    "id": "node1",
                    "metadata": {},
                    "embedding": [],
                    "entities": [],
                    "tags": [],
                },
            },
            "relationships": [{"source_id": "node1", "target_id": "nonexistent", "relationship_type": "related"}],
        }

        # Act
        graph = Graph.from_dict(graph_data)

        # Assert
        assert len(graph) == 1
        assert len(graph.relationships) == 0  # Relationship should be skipped

    def test_graph_dunder_methods(self):
        """Test graph dunder methods (__len__, __iter__, __contains__, __repr__)."""
        # Arrange
        graph = Graph()
        node1 = Node(text="Node 1")
        node2 = Node(text="Node 2")
        graph.add_node(node1)
        graph.add_node(node2)

        # Test __len__
        assert len(graph) == 2

        # Test __iter__
        nodes_list = list(graph)
        assert len(nodes_list) == 2
        assert node1 in nodes_list
        assert node2 in nodes_list

        # Test __contains__
        assert node1.id in graph
        assert node2.id in graph
        assert "nonexistent" not in graph

        # Test __repr__
        repr_str = repr(graph)
        assert repr_str == "Graph(nodes=2, relationships=0)"

    def test_relationship_index_rebuild(self):
        """Test that relationship index is properly maintained."""
        # Arrange
        graph = Graph()
        node1 = Node(text="Node 1")
        node2 = Node(text="Node 2")
        graph.add_node(node1)
        graph.add_node(node2)

        rel = Relationship(source_id=node1.id, target_id=node2.id, relationship_type="related")
        graph.add_relationship(rel)

        # Act - manually rebuild index
        graph._rebuild_relationship_index()

        # Assert
        assert rel in graph._relationship_index[node1.id]
        assert rel in graph._relationship_index[node2.id]


class TestNodeProcessorError:
    """Test custom NodeProcessorError exception."""

    def test_node_processor_error_creation(self):
        """Test creating NodeProcessorError with message."""
        # Act
        error = NodeProcessorError("Test error message")

        # Assert
        assert str(error) == "Test error message"
        assert isinstance(error, Exception)

    def test_node_processor_error_with_cause(self):
        """Test NodeProcessorError with underlying cause."""
        # Arrange
        original_error = ValueError("Original error")

        def raise_wrapper_error():
            """Helper function to raise NodeProcessorError with cause."""
            raise NodeProcessorError("Wrapper error") from original_error

        # Act & Assert
        try:
            raise_wrapper_error()
        except NodeProcessorError as error:
            assert str(error) == "Wrapper error"
            assert isinstance(error.__cause__, ValueError)
            assert str(error.__cause__) == "Original error"


class TestEdgeCasesAndErrorHandling:
    """Test edge cases and error handling scenarios."""

    def test_graph_with_large_number_of_nodes(self):
        """Test graph performance with many nodes."""
        # Arrange
        graph = Graph()
        nodes = [Node(text=f"Node {i}") for i in range(100)]

        # Act
        for node in nodes:
            graph.add_node(node)

        # Assert
        assert len(graph) == 100
        assert len(list(graph)) == 100

    def test_circular_relationships(self):
        """Test handling of circular relationships."""
        # Arrange
        graph = Graph()
        node1 = Node(text="Node 1")
        node2 = Node(text="Node 2")
        node3 = Node(text="Node 3")

        for node in [node1, node2, node3]:
            graph.add_node(node)

        # Create circular relationships: 1 -> 2 -> 3 -> 1
        graph.add_relationship(Relationship(source_id=node1.id, target_id=node2.id, relationship_type="next"))
        graph.add_relationship(Relationship(source_id=node2.id, target_id=node3.id, relationship_type="next"))
        graph.add_relationship(Relationship(source_id=node3.id, target_id=node1.id, relationship_type="next"))

        # Act
        neighbors = graph.get_neighbors(node1.id, distance=3)

        # Assert
        # Should handle circular relationships without infinite loops
        assert len(neighbors) == 2  # node2 and node3

    def test_self_referencing_relationship(self):
        """Test adding relationship from node to itself."""
        # Arrange
        graph = Graph()
        node = Node(text="Self-referencing node")
        graph.add_node(node)

        rel = Relationship(source_id=node.id, target_id=node.id, relationship_type="self")

        # Act
        result = graph.add_relationship(rel)

        # Assert
        assert result is True
        assert len(graph.relationships) == 1
        neighbors = graph.get_neighbors(node.id)
        assert len(neighbors) == 0  # Self-relationship doesn't add neighbors

    @pytest.mark.asyncio
    async def test_concurrent_node_creation(self):
        """Test concurrent node creation with NodeFactory."""
        # Arrange
        factory = NodeFactory()
        texts = [f"Concurrent node {i}" for i in range(10)]

        # Act
        tasks = [factory.create_node(text) for text in texts]
        nodes = await asyncio.gather(*tasks)

        # Assert
        assert len(nodes) == 10
        assert all(isinstance(node, Node) for node in nodes)
        assert len({node.id for node in nodes}) == 10  # All unique IDs
