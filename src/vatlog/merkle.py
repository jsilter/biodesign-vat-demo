"""RFC 6962 Merkle tree over canonical record bytes.

leaf = SHA256(0x00 || record_bytes), node = SHA256(0x01 || left || right).

Backed by pymerkle, whose default ``security=True`` mode applies exactly those domain
separation prefixes. This wrapper exists for two reasons: the rest of the package counts
leaves from 0 while pymerkle counts from 1, and proofs cross a process boundary as JSON
(they travel inside a bundle), so they are carried as pymerkle's own ``serialize()`` dict
and rebuilt with ``MerkleProof.deserialize`` rather than hand-constructed.

Note ``verify_inclusion`` takes the *leaf hash*, not the raw entry bytes.
"""

from __future__ import annotations

from pymerkle import InmemoryTree, verify_consistency, verify_inclusion
from pymerkle.proof import InvalidProof, MerkleProof


class RecordTree:
    """Append-only Merkle tree. Leaves are canonical record bytes, in append order."""

    def __init__(self) -> None:
        self._tree = InmemoryTree(algorithm="sha256")

    def append(self, leaf_bytes: bytes) -> int:
        """Append a leaf; returns its 0-based index."""
        self._tree.append_entry(leaf_bytes)
        return self._tree.get_size() - 1

    @property
    def size(self) -> int:
        return self._tree.get_size()

    @property
    def root_hex(self) -> str:
        if self.size == 0:
            return self._tree.hash_empty().hex()
        return self._tree.get_state().hex()

    def leaf_hash(self, leaf_bytes: bytes) -> bytes:
        return self._tree.hash_buff(leaf_bytes)

    def prove_inclusion(self, index: int) -> dict:
        """Inclusion proof for the 0-based ``index``, as a JSON-able dict."""
        proof = self._tree.prove_inclusion(index + 1).serialize()
        proof["leaf_index"] = index
        return proof

    def prove_consistency(self, earlier_size: int) -> dict:
        proof = self._tree.prove_consistency(earlier_size).serialize()
        proof["earlier_size"] = earlier_size
        return proof

    def root_at(self, size: int) -> str:
        """Root the tree had at ``size`` leaves. Used to check a consistency proof."""
        return self._tree.prove_consistency(size).retrieve_prior_state().hex()

    @classmethod
    def from_leaves(cls, leaves: list[bytes]) -> "RecordTree":
        tree = cls()
        for leaf in leaves:
            tree.append(leaf)
        return tree


def leaf_hash(leaf_bytes: bytes) -> bytes:
    """RFC 6962 leaf hash, standalone (a verifier has no tree)."""
    return InmemoryTree(algorithm="sha256").hash_buff(leaf_bytes)


def check_inclusion(
    leaf_bytes: bytes, root_hex: str, proof: dict, *, tree_size: int | None = None,
) -> bool:
    """Verify membership at the claimed position with the fixed RFC 6962 hash profile.

    Pass the signed ``tree_size`` when verifying a bundle. Pymerkle verifies the hashes
    but does not bind its proof metadata to a tree size or leaf position, so validate the
    path shape here before delegating the cryptographic calculation.
    """
    try:
        metadata = proof["metadata"]
        size, index = metadata["size"], proof["leaf_index"]
        if (metadata["algorithm"] != "sha256" or metadata["security"] is not True
                or type(size) is not int or type(index) is not int or not 0 <= index < size
                or (tree_size is not None and size != tree_size)):
            return False
        # RFC 6962 splits at the largest power of two strictly smaller than the size.
        directions = []
        while size > 1:
            split = 1 << ((size - 1).bit_length() - 1)
            if index < split:
                directions.append(0)
                size = split
            else:
                directions.append(1)
                index -= split
                size -= split
        expected_rule = list(reversed(directions)) + [0]
        if (proof["rule"] != expected_rule or any(type(bit) is not int for bit in proof["rule"])
                or proof["subset"] != [] or not isinstance(proof["path"], list)
                or len(proof["path"]) != len(expected_rule)
                or any(not isinstance(d, str) or len(d) != 64 for d in proof["path"])):
            return False
        verify_inclusion(
            leaf_hash(leaf_bytes),
            bytes.fromhex(root_hex),
            MerkleProof.deserialize(proof),
        )
        return True
    except (InvalidProof, KeyError, ValueError, TypeError, IndexError):
        return False


def check_consistency(earlier_root_hex: str, later_root_hex: str, proof: dict,
                      *, earlier_size: int | None = None, later_size: int | None = None) -> bool:
    """Bind consistency proof shape to both tree sizes before checking hashes."""
    try:
        metadata = proof["metadata"]
        first, last = proof["earlier_size"], metadata["size"]
        if (type(first) is not int or type(last) is not int or not 0 < first <= last
                or last > 2**53 - 1 or metadata["algorithm"] != "sha256"
                or metadata["security"] is not True
                or (earlier_size is not None and first != earlier_size)
                or (later_size is not None and last != later_size)):
            return False
        # Size-derived pymerkle consistency traversal, with no leaf allocation.
        offset, limit, bit, stack = first, last, 0, []
        while offset != limit and not (offset == 0 and limit == 1):
            split = 1 << ((limit - 1).bit_length() - 1)
            if offset < split:
                stack.append((bit, 0))
                limit, bit = split, 0
            else:
                stack.append((bit, 1))
                offset, limit, bit = offset - split, limit - split, 1
        rule, subset = [bit], [int(offset == limit)]
        for direction, mask in reversed(stack):
            rule.append(direction)
            subset.append(mask)
        if (proof["rule"] != rule or proof["subset"] != subset
                or any(type(x) is not int for x in proof["rule"] + proof["subset"])
                or len(proof["path"]) != len(rule)
                or any(not isinstance(x, str) or len(x) != 64 for x in proof["path"])):
            return False
        verify_consistency(
            bytes.fromhex(earlier_root_hex),
            bytes.fromhex(later_root_hex),
            MerkleProof.deserialize(proof),
        )
        return True
    except (InvalidProof, KeyError, ValueError, TypeError, IndexError):
        return False
