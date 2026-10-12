import mimetypes
import os
import time
from datetime import datetime

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Evidence, ReverseSearchLog, VerificationCase

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
VIDEO_EXTENSIONS = {'.mp4', '.webm', '.mov'}
VIDEO_LIMIT_MULTIPLIER = 5  # videos may be 5x the image limit

# Seconds to wait before each attempt (first attempt is immediate)
GEMINI_RETRY_DELAYS = [0, 3, 6]
# HTTP codes that mean "try again later" rather than "your request is wrong"
GEMINI_TRANSIENT_CODES = {429, 500, 503, 504}

GEMINI_SYSTEM_INSTRUCTION = (
    'You are an assistant for a media verification tool used by students and fact-checkers. '
    'You give cautious, evidence-based observations to help a human reviewer. '
    'You never claim that media is definitely real or definitely fake, because you cannot prove it. '
    'You never identify real people from their faces. '
    'Write plain text only, with no markdown symbols such as asterisks, hashes or backticks.'
)


class GeminiBusyError(Exception):
    """Gemini is overloaded or rate limited. Safe to try again later."""


# ---------------------------------------------------------------
# Small validation helpers (replacing forms.py)
# ---------------------------------------------------------------
def _parse_date(value, label):
    """Return (date or None, error or None)."""
    value = (value or '').strip()
    if not value:
        return None, None
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return None, f'{label}: enter a valid date.'
    if parsed > timezone.localdate():
        return None, f'{label} cannot be in the future.'
    return parsed, None


def _check_url(value, label):
    """Return an error string, or None if the URL is empty or valid."""
    value = (value or '').strip()
    if not value:
        return None
    try:
        URLValidator(schemes=['http', 'https'])(value)
    except ValidationError:
        return f'{label}: enter a valid link starting with http:// or https://'
    return None


def _clean_case_data(post, files):
    """Validate the new-case form. Returns (cleaned_data, errors)."""
    errors = {}

    title = post.get('title', '').strip()
    claim = post.get('claim_description', '').strip()
    claimed_source = post.get('claimed_source', '').strip()
    source_url = post.get('source_url', '').strip()
    provenance = post.get('provenance_notes', '').strip()

    if not title:
        errors['title'] = 'Title is required.'
    elif len(title) > 200:
        errors['title'] = 'Title must be 200 characters or fewer.'

    if not claim:
        errors['claim_description'] = 'Describe what the media claims to show.'

    if len(claimed_source) > 200:
        errors['claimed_source'] = 'Source must be 200 characters or fewer.'

    url_error = _check_url(source_url, 'Source link')
    if url_error:
        errors['source_url'] = url_error

    pub_date, date_error = _parse_date(post.get('publication_date'), 'Publication date')
    if date_error:
        errors['publication_date'] = date_error

    media = files.get('media_file')
    media_type = VerificationCase.MEDIA_IMAGE
    if not media:
        errors['media_file'] = 'Please choose an image or video file.'
    else:
        name = media.name.lower()
        extension = name[name.rfind('.'):] if '.' in name else ''
        limit_mb = settings.MAX_UPLOAD_MB
        if extension in IMAGE_EXTENSIONS:
            media_type = VerificationCase.MEDIA_IMAGE
        elif extension in VIDEO_EXTENSIONS:
            media_type = VerificationCase.MEDIA_VIDEO
            limit_mb = settings.MAX_UPLOAD_MB * VIDEO_LIMIT_MULTIPLIER
        else:
            errors['media_file'] = 'Unsupported file type. Use JPG, PNG, WEBP, GIF, MP4, WEBM or MOV.'
        if 'media_file' not in errors and media.size > limit_mb * 1024 * 1024:
            errors['media_file'] = f'File is too large. The limit for this type is {limit_mb} MB.'

    cleaned = {
        'title': title,
        'claim_description': claim,
        'claimed_source': claimed_source,
        'source_url': source_url,
        'publication_date': pub_date,
        'provenance_notes': provenance,
        'media_file': media,
        'media_type': media_type,
    }
    return cleaned, errors


def _case_url(case, anchor=''):
    return f'{case.get_absolute_url()}{anchor}'


def _move_to_verifying(case):
    """Once work starts on a case, move it from Open to Under Verification."""
    if case.status == VerificationCase.STATUS_OPEN:
        case.status = VerificationCase.STATUS_VERIFYING
        case.save(update_fields=['status', 'updated_at'])


# ---------------------------------------------------------------
# Gemini analysis
# ---------------------------------------------------------------
def _build_prompt(case):
    credibility = case.credibility
    checklist_lines = '\n'.join(
        f"- {row['label']}: {row['points']:+d} ({row['note']})"
        for row in credibility['breakdown']
    )
    searches = '\n'.join(
        f"- {log.get_engine_display()}: "
        f"{'match found' if log.result_found else 'no match'}"
        f"{', earliest date ' + str(log.earliest_date) if log.earliest_date else ''}"
        for log in case.search_logs.all()
    ) or '- None logged'
    evidence = '\n'.join(
        f'- [{e.get_stance_display()}] {e.title} ({e.get_evidence_type_display()})'
        for e in case.evidence_items.all()
    ) or '- None recorded'

    attached = 'The image is attached.' if case.is_image else 'This is a video; no frames are attached.'

    return f"""A reviewer is verifying a piece of media. {attached}

CLAIM: {case.claim_description}
CLAIMED SOURCE: {case.claimed_source or 'not recorded'}
SOURCE LINK: {case.source_url or 'not recorded'}
CLAIMED PUBLICATION DATE: {case.publication_date or 'not recorded'}
PROVENANCE NOTES: {case.provenance_notes or 'none'}

REVERSE SEARCHES LOGGED:
{searches}

EVIDENCE RECORDED:
{evidence}

RULE-BASED CHECKLIST (score {credibility['score']}/100, guidance only):
{checklist_lines}

Write a short analysis of about 250 words with exactly these five sections, each starting with the label in capital letters:
VISUAL OBSERVATIONS: what is visible, only if an image is attached, otherwise say no visual was analysed.
CONSISTENCY WITH THE CLAIM: does the content plausibly match the claim and the recorded details?
POSSIBLE WARNING SIGNS: anything that could indicate editing, AI generation, recycling or misdating. State clearly that these are uncertain.
WHAT TO CHECK NEXT: two or three concrete verification steps.
LIMITATIONS: why this analysis cannot prove authenticity and why a human must decide."""


def _is_transient_error(exc):
    """True for overload and rate limit errors that are worth retrying."""
    code = getattr(exc, 'code', None)
    if code in GEMINI_TRANSIENT_CODES:
        return True
    text = str(exc).upper()
    return any(word in text for word in ('UNAVAILABLE', 'OVERLOADED', 'RESOURCE_EXHAUSTED', 'HIGH DEMAND'))


def _generate_with_retry(client, model, contents, config):
    """Call one model, retrying a few times if Gemini is busy."""
    last_error = None
    for delay in GEMINI_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            return client.models.generate_content(model=model, contents=contents, config=config)
        except Exception as exc:
            if not _is_transient_error(exc):
                raise
            last_error = exc
    raise GeminiBusyError(str(last_error))


def run_gemini_analysis(case):
    """Call Gemini and return the analysis text. Raises on failure."""
    if not settings.GEMINI_API_KEY:
        raise RuntimeError('GEMINI_API_KEY is missing. Add it to the .env file and restart the server.')

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.GEMINI_API_KEY)

    contents = []
    if case.is_image:
        mime_type = mimetypes.guess_type(case.media_file.name)[0] or 'image/jpeg'
        with case.media_file.open('rb') as handle:
            contents.append(types.Part.from_bytes(data=handle.read(), mime_type=mime_type))
    contents.append(_build_prompt(case))

    config = types.GenerateContentConfig(
        system_instruction=GEMINI_SYSTEM_INSTRUCTION,
        temperature=0.2,
    )

    # Main model first, then an optional backup model from .env
    models_to_try = [settings.GEMINI_MODEL]
    fallback = os.getenv('GEMINI_FALLBACK_MODEL', '').strip()
    if fallback and fallback != settings.GEMINI_MODEL:
        models_to_try.append(fallback)

    response = None
    for model in models_to_try:
        try:
            response = _generate_with_retry(client, model, contents, config)
            break
        except GeminiBusyError:
            continue

    if response is None:
        raise GeminiBusyError('Gemini stayed busy after several attempts.')

    text = (response.text or '').strip()
    if not text:
        raise RuntimeError('Gemini returned an empty response.')
    return text


# ---------------------------------------------------------------
# Views
# ---------------------------------------------------------------
def home(request):
    """Dashboard: list of cases with search, status filter and chart data."""
    all_cases = VerificationCase.objects.prefetch_related('evidence_items', 'search_logs')

    query = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()

    cases = all_cases
    if query:
        cases = cases.filter(
            Q(title__icontains=query)
            | Q(claim_description__icontains=query)
            | Q(claimed_source__icontains=query)
        )
    if status in dict(VerificationCase.STATUS_CHOICES):
        cases = cases.filter(status=status)

    verdict_counts = {
        'Likely authentic': 0,
        'Inconclusive': 0,
        'Likely manipulated or misattributed': 0,
    }
    status_counts = {label: 0 for _, label in VerificationCase.STATUS_CHOICES}
    for case in all_cases:
        verdict_counts[case.credibility['verdict']] += 1
        status_counts[case.get_status_display()] += 1

    context = {
        'cases': cases,
        'total_cases': all_cases.count(),
        'query': query,
        'selected_status': status,
        'status_choices': VerificationCase.STATUS_CHOICES,
        'chart_data': {
            'verdict_labels': list(verdict_counts.keys()),
            'verdict_values': list(verdict_counts.values()),
            'status_labels': list(status_counts.keys()),
            'status_values': list(status_counts.values()),
        },
    }
    return render(request, 'verification/home.html', context)


def case_create(request):
    errors, values = {}, {}
    if request.method == 'POST':
        values = request.POST.dict()
        cleaned, errors = _clean_case_data(request.POST, request.FILES)
        if not errors:
            case = VerificationCase.objects.create(**cleaned)
            messages.success(request, f'Case {case.tracking_id} created. Now add reverse searches and evidence.')
            return redirect(case.get_absolute_url())
        messages.error(request, 'Please fix the highlighted problems and try again.')

    context = {
        'errors': errors,
        'values': values,
        'max_image_mb': settings.MAX_UPLOAD_MB,
        'max_video_mb': settings.MAX_UPLOAD_MB * VIDEO_LIMIT_MULTIPLIER,
    }
    return render(request, 'verification/case_form.html', context)


def case_detail(request, pk):
    case = get_object_or_404(
        VerificationCase.objects.prefetch_related('evidence_items', 'search_logs'), pk=pk
    )
    context = {
        'case': case,
        'credibility': case.credibility,
        'evidence_items': case.evidence_items.all(),
        'search_logs': case.search_logs.all(),
        'evidence_type_choices': Evidence.TYPE_CHOICES,
        'stance_choices': Evidence.STANCE_CHOICES,
        'engine_choices': ReverseSearchLog.ENGINE_CHOICES,
        'status_choices': VerificationCase.STATUS_CHOICES,
        'ai_ready': bool(settings.GEMINI_API_KEY),
    }
    return render(request, 'verification/case_detail.html', context)


@require_POST
def case_add_evidence(request, pk):
    case = get_object_or_404(VerificationCase, pk=pk)

    title = request.POST.get('title', '').strip()
    evidence_type = request.POST.get('evidence_type', '')
    stance = request.POST.get('stance', '')
    url = request.POST.get('url', '').strip()
    note = request.POST.get('note', '').strip()

    problems = []
    if not title:
        problems.append('Evidence title is required.')
    elif len(title) > 200:
        problems.append('Evidence title must be 200 characters or fewer.')
    if evidence_type not in dict(Evidence.TYPE_CHOICES):
        problems.append('Choose a valid evidence type.')
    if stance not in dict(Evidence.STANCE_CHOICES):
        problems.append('Choose whether the evidence supports or contradicts the claim.')
    url_error = _check_url(url, 'Evidence link')
    if url_error:
        problems.append(url_error)

    if problems:
        for problem in problems:
            messages.error(request, problem)
    else:
        Evidence.objects.create(
            case=case, title=title, evidence_type=evidence_type,
            stance=stance, url=url, note=note,
        )
        _move_to_verifying(case)
        messages.success(request, 'Evidence added.')
    return redirect(_case_url(case, '#evidence'))


@require_POST
def case_add_search(request, pk):
    case = get_object_or_404(VerificationCase, pk=pk)

    engine = request.POST.get('engine', '')
    result_found = request.POST.get('result_found') == 'on'
    matched_url = request.POST.get('matched_url', '').strip()
    notes = request.POST.get('notes', '').strip()
    earliest_date, date_error = _parse_date(request.POST.get('earliest_date'), 'Earliest date')

    problems = []
    if engine not in dict(ReverseSearchLog.ENGINE_CHOICES):
        problems.append('Choose a valid search engine.')
    if date_error:
        problems.append(date_error)
    url_error = _check_url(matched_url, 'Matched link')
    if url_error:
        problems.append(url_error)

    if problems:
        for problem in problems:
            messages.error(request, problem)
    else:
        ReverseSearchLog.objects.create(
            case=case, engine=engine, result_found=result_found,
            earliest_date=earliest_date if result_found else None,
            matched_url=matched_url, notes=notes,
        )
        _move_to_verifying(case)
        messages.success(request, 'Reverse search logged.')
    return redirect(_case_url(case, '#searches'))


@require_POST
def case_update_status(request, pk):
    case = get_object_or_404(VerificationCase, pk=pk)
    new_status = request.POST.get('status', '')
    if new_status in dict(VerificationCase.STATUS_CHOICES):
        case.status = new_status
        case.save(update_fields=['status', 'updated_at'])
        messages.success(request, f'Status changed to {case.get_status_display()}.')
    else:
        messages.error(request, 'Invalid status.')
    return redirect(case.get_absolute_url())


@require_POST
def case_analyze(request, pk):
    case = get_object_or_404(VerificationCase, pk=pk)
    try:
        case.ai_analysis = run_gemini_analysis(case)
        case.ai_analyzed_at = timezone.now()
        case.save(update_fields=['ai_analysis', 'ai_analyzed_at', 'updated_at'])
        messages.success(request, 'AI analysis complete. Treat it as guidance, not proof.')
    except GeminiBusyError:
        messages.error(
            request,
            'Gemini is very busy right now (Google server overload, not a problem with your case). '
            'Your case is saved. Please wait a minute and click the AI analysis button again.'
        )
    except Exception as exc:  # keep the demo alive whatever the API does
        messages.error(request, f'AI analysis failed: {str(exc)[:300]}')
    return redirect(_case_url(case, '#analysis'))


@require_POST
def case_delete(request, pk):
    case = get_object_or_404(VerificationCase, pk=pk)
    tracking_id = case.tracking_id
    case.media_file.delete(save=False)
    case.delete()
    messages.success(request, f'Case {tracking_id} deleted.')
    return redirect('home')


def case_report(request, pk):
    """Printable verification report for one case."""
    case = get_object_or_404(
        VerificationCase.objects.prefetch_related('evidence_items', 'search_logs'), pk=pk
    )
    context = {
        'case': case,
        'credibility': case.credibility,
        'evidence_items': case.evidence_items.all(),
        'search_logs': case.search_logs.all(),
        'generated_at': timezone.localtime(),
    }
    return render(request, 'verification/report.html', context)


def guide(request):
    """Static 'How to Verify Media' page (built by a teammate)."""
    return render(request, 'verification/guide.html')