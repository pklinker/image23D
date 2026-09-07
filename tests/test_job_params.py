"""Job parameter tests: typed job params and the optional subject bbox.

The governing rule for `bbox`: absent means "behave exactly as before", present
means "crop to this region", and *invalid* means reject loudly. Silently
dropping a malformed bbox would hand the caller a plausible-looking model built
from the wrong region, with no signal that their integration is broken.
"""
import io
import uuid

import pytest
from PIL import Image

from common.schemas import (
    DEFAULT_FACE_COUNT,
    MAX_FACE_COUNT,
    MIN_FACE_COUNT,
    SAMPLER_NODES,
    JobCreateRequest,
    JobParams,
)
from worker.app.pipeline import (
    MIN_CROP_PIXELS,
    PipelineError,
    load_patched_graph,
    prepare_input_image,
)


def _image_bytes(width=800, height=600, fmt="PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buffer, format=fmt)
    return buffer.getvalue()


def _size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as img:
        return img.size


# --- defaults and validation ---------------------------------------------


def test_params_are_optional_entirely():
    """The viewer sends no params at all; that must stay valid."""
    request = JobCreateRequest(object_key="uploads/x.png")
    assert request.params.bbox is None
    assert request.params.seed is None
    assert request.params.target_face_count == DEFAULT_FACE_COUNT


def test_unknown_parameter_is_rejected():
    """A misspelled knob should be heard about immediately, not silently
    ignored until someone wonders why their setting never applied."""
    with pytest.raises(ValueError, match="extra_forbidden|Extra inputs"):
        JobParams(target_face_counts=100_000)


@pytest.mark.parametrize("count", [MIN_FACE_COUNT - 1, MAX_FACE_COUNT + 1, 0, -5])
def test_face_count_is_bounded(count):
    with pytest.raises(ValueError):
        JobParams(target_face_count=count)


@pytest.mark.parametrize(
    "bbox",
    [
        [0.5, 0.1, 0.2, 0.9],   # x1 <= x0
        [0.1, 0.9, 0.5, 0.2],   # y1 <= y0
        [0.1, 0.1, 1.5, 0.9],   # outside 0-1
        [-0.1, 0.1, 0.5, 0.9],  # negative
        [0.3, 0.1, 0.3, 0.9],   # zero width
        [0.1, 0.2, 0.5],        # too few values
    ],
)
def test_invalid_bbox_is_rejected(bbox):
    with pytest.raises(ValueError):
        JobParams(bbox=bbox)


def test_valid_bbox_is_accepted():
    assert JobParams(bbox=[0.0, 0.0, 1.0, 1.0]).bbox == [0.0, 0.0, 1.0, 1.0]
    assert JobParams(bbox=[0.25, 0.1, 0.75, 0.95]).bbox == [0.25, 0.1, 0.75, 0.95]


# --- graph patching -------------------------------------------------------


def test_default_params_leave_the_stock_seeds_alone():
    """Reproducibility: every timing and quality measurement so far was taken
    with the graph's own seeds."""
    stock = load_patched_graph(uuid.uuid4(), "x.png")
    seeds = [stock[node]["inputs"]["seed"] for node in SAMPLER_NODES]

    assert seeds == [56, 42, 42, 43]


def test_seed_is_applied_to_every_sampler_when_asked_for():
    graph = load_patched_graph(uuid.uuid4(), "x.png", JobParams(seed=1234))
    assert [graph[node]["inputs"]["seed"] for node in SAMPLER_NODES] == [1234] * 4


def test_target_face_count_reaches_the_decimate_node():
    graph = load_patched_graph(uuid.uuid4(), "x.png", JobParams(target_face_count=120_000))
    assert graph["186"]["inputs"]["target_face_count"] == 120_000


def test_default_face_count_matches_the_tuned_value():
    graph = load_patched_graph(uuid.uuid4(), "x.png")
    assert graph["186"]["inputs"]["target_face_count"] == DEFAULT_FACE_COUNT


# --- bbox cropping --------------------------------------------------------


def test_no_bbox_returns_the_bytes_untouched():
    """The default path must be byte-for-byte what it was before."""
    original = _image_bytes()
    assert prepare_input_image(original, JobParams()) is original


def test_bbox_crops_to_the_requested_region():
    original = _image_bytes(1000, 1000)
    # Middle half, with the 1.1 pad factor applied around it -> 550px.
    cropped = prepare_input_image(original, JobParams(bbox=[0.25, 0.25, 0.75, 0.75]))

    width, height = _size(cropped)
    assert (width, height) == (550, 550)


def test_bbox_padding_is_clamped_to_the_image():
    """Padding a box that already touches the edge must not overflow."""
    original = _image_bytes(400, 400)
    cropped = prepare_input_image(original, JobParams(bbox=[0.0, 0.0, 1.0, 1.0]))

    assert _size(cropped) == (400, 400)


def test_bbox_crop_is_smaller_than_the_source():
    original = _image_bytes(1200, 900)
    cropped = prepare_input_image(original, JobParams(bbox=[0.4, 0.3, 0.6, 0.7]))

    assert _size(cropped)[0] < 1200
    assert _size(cropped)[1] < 900


def test_a_tiny_bbox_is_rejected_rather_than_run():
    """A few pixels cannot be reconstructed; better to say so than to spend a
    GPU slot producing nonsense."""
    original = _image_bytes(1000, 1000)
    with pytest.raises(PipelineError, match=f"{MIN_CROP_PIXELS}px minimum"):
        prepare_input_image(original, JobParams(bbox=[0.5, 0.5, 0.51, 0.51]))


def test_cropped_output_is_a_readable_png():
    cropped = prepare_input_image(_image_bytes(800, 800, fmt="JPEG"), JobParams(bbox=[0.2, 0.2, 0.8, 0.8]))
    with Image.open(io.BytesIO(cropped)) as img:
        assert img.format == "PNG"
        assert img.mode == "RGB"


def test_exif_rotation_is_applied_before_cropping():
    """LoadImage applies exif_transpose, so a bbox given against the displayed
    image only lines up if we transpose first. A portrait photo tagged as
    rotated must come out with the displayed orientation's aspect."""
    buffer = io.BytesIO()
    image = Image.new("RGB", (900, 300), (200, 30, 30))
    exif = image.getexif()
    exif[274] = 6  # Orientation: rotate 90 CW -> displayed as 300x900
    image.save(buffer, format="JPEG", exif=exif)

    cropped = prepare_input_image(buffer.getvalue(), JobParams(bbox=[0.0, 0.0, 1.0, 1.0]))

    width, height = _size(cropped)
    assert (width, height) == (300, 900), "bbox must be applied to the displayed orientation"
