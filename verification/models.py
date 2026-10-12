from django.db import models
from django.urls import reverse
from django.utils.functional import cached_property


class VerificationCase(models.Model):
    """One image or video that a user is trying to verify."""

    MEDIA_IMAGE = 'image'
    MEDIA_VIDEO = 'video'
    MEDIA_CHOICES = [
        (MEDIA_IMAGE, 'Image'),
        (MEDIA_VIDEO, 'Video'),
    ]

    STATUS_OPEN = 'open'
    STATUS_VERIFYING = 'verifying'
    STATUS_CONCLUDED = 'concluded'
    STATUS_CHOICES = [
        (STATUS_OPEN, 'Open'),
        (STATUS_VERIFYING, 'Under Verification'),
        (STATUS_CONCLUDED, 'Concluded'),
    ]

    title = models.CharField(max_length=200)
    claim_description = models.TextField(
        help_text='What does this media claim to show? (event, place, person, date)'
    )
    media_file = models.FileField(upload_to='cases/%Y/%m/')
    media_type = models.CharField(max_length=10, choices=MEDIA_CHOICES, default=MEDIA_IMAGE)

    claimed_source = models.CharField(
        max_length=200, blank=True,
        help_text='Who posted or supplied this media (account, outlet, person)?'
    )
    source_url = models.URLField(blank=True, help_text='Link to where it was first seen')
    publication_date = models.DateField(
        null=True, blank=True, help_text='Date the source claims it was published'
    )
    provenance_notes = models.TextField(
        blank=True,
        help_text='Origin details: original uploader, camera or device info, EXIF, chain of custody'
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN)

    ai_analysis = models.TextField(blank=True)
    ai_analyzed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.tracking_id}: {self.title}'

    def get_absolute_url(self):
        return reverse('case_detail', args=[self.pk])

    @property
    def tracking_id(self):
        return f'VER-{self.pk:04d}' if self.pk else 'VER-NEW'

    @property
    def is_image(self):
        return self.media_type == self.MEDIA_IMAGE

    @cached_property
    def credibility(self):
        """
        Transparent rule-based score from 0 to 100.
        This is guidance for a human reviewer, never proof of authenticity.
        Returns a dict: score, verdict, css_class and breakdown (list of rows).
        """
        breakdown = []

        def add(label, points, note):
            breakdown.append({'label': label, 'points': points, 'note': note})

        # --- Provenance checklist (max 50) ---
        if self.claimed_source.strip():
            add('Source identified', 15, f'Claimed source: {self.claimed_source}')
        else:
            add('Source identified', 0, 'No source recorded')

        if self.source_url:
            add('Source link provided', 10, 'A first-seen link was recorded')
        else:
            add('Source link provided', 0, 'No link recorded')

        if self.provenance_notes.strip():
            add('Provenance notes recorded', 15, 'Origin details were documented')
        else:
            add('Provenance notes recorded', 0, 'No provenance details')

        if self.publication_date:
            add('Publication date recorded', 10, f'Claimed date: {self.publication_date}')
        else:
            add('Publication date recorded', 0, 'No publication date')

        # --- Reverse search (max 20, penalty 20) ---
        logs = list(self.search_logs.all())
        if logs:
            add('Reverse search performed', 20, f'{len(logs)} search(es) logged')
        else:
            add('Reverse search performed', 0, 'No reverse search logged')

        if self.publication_date:
            earlier = [
                log for log in logs
                if log.result_found and log.earliest_date
                and log.earliest_date < self.publication_date
            ]
            if earlier:
                first = min(log.earliest_date for log in earlier)
                add(
                    'Earlier appearance found', -20,
                    f'Seen online on {first}, before the claimed date. '
                    'The media may be recycled or misdated.'
                )

        # --- Corroborating evidence (max 30, penalty 30) ---
        items = list(self.evidence_items.all())
        supports = sum(1 for e in items if e.stance == Evidence.STANCE_SUPPORTS)
        contradicts = sum(1 for e in items if e.stance == Evidence.STANCE_CONTRADICTS)

        support_points = min(supports, 3) * 10
        add('Supporting evidence', support_points, f'{supports} supporting item(s), up to 3 count')

        if contradicts:
            add(
                'Contradicting evidence', -min(contradicts, 2) * 15,
                f'{contradicts} contradicting item(s), up to 2 count'
            )

        score = max(0, min(100, sum(row['points'] for row in breakdown)))

        if score >= 70:
            verdict, css_class = 'Likely authentic', 'good'
        elif score >= 40:
            verdict, css_class = 'Inconclusive', 'warn'
        else:
            verdict, css_class = 'Likely manipulated or misattributed', 'bad'

        return {
            'score': score,
            'verdict': verdict,
            'css_class': css_class,
            'breakdown': breakdown,
        }


class Evidence(models.Model):
    """A piece of corroborating or contradicting evidence for a case."""

    TYPE_NEWS = 'news'
    TYPE_OFFICIAL = 'official'
    TYPE_EXPERT = 'expert'
    TYPE_METADATA = 'metadata'
    TYPE_OTHER = 'other'
    TYPE_CHOICES = [
        (TYPE_NEWS, 'News report'),
        (TYPE_OFFICIAL, 'Official statement'),
        (TYPE_EXPERT, 'Expert opinion'),
        (TYPE_METADATA, 'Metadata or technical check'),
        (TYPE_OTHER, 'Other'),
    ]

    STANCE_SUPPORTS = 'supports'
    STANCE_CONTRADICTS = 'contradicts'
    STANCE_NEUTRAL = 'neutral'
    STANCE_CHOICES = [
        (STANCE_SUPPORTS, 'Supports the claim'),
        (STANCE_CONTRADICTS, 'Contradicts the claim'),
        (STANCE_NEUTRAL, 'Neutral'),
    ]

    case = models.ForeignKey(
        VerificationCase, on_delete=models.CASCADE, related_name='evidence_items'
    )
    evidence_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default=TYPE_NEWS)
    title = models.CharField(max_length=200)
    url = models.URLField(blank=True)
    note = models.TextField(blank=True)
    stance = models.CharField(max_length=20, choices=STANCE_CHOICES, default=STANCE_NEUTRAL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name_plural = 'Evidence'

    def __str__(self):
        return f'{self.title} ({self.get_stance_display()})'


class ReverseSearchLog(models.Model):
    """A reverse image or video search the user ran, and what it found."""

    ENGINE_GOOGLE = 'google_lens'
    ENGINE_TINEYE = 'tineye'
    ENGINE_BING = 'bing'
    ENGINE_YANDEX = 'yandex'
    ENGINE_INVID = 'invid'
    ENGINE_OTHER = 'other'
    ENGINE_CHOICES = [
        (ENGINE_GOOGLE, 'Google Lens'),
        (ENGINE_TINEYE, 'TinEye'),
        (ENGINE_BING, 'Bing Visual Search'),
        (ENGINE_YANDEX, 'Yandex Images'),
        (ENGINE_INVID, 'InVID / WeVerify'),
        (ENGINE_OTHER, 'Other'),
    ]

    case = models.ForeignKey(
        VerificationCase, on_delete=models.CASCADE, related_name='search_logs'
    )
    engine = models.CharField(max_length=20, choices=ENGINE_CHOICES, default=ENGINE_GOOGLE)
    result_found = models.BooleanField(
        default=False, help_text='Did the search find this media elsewhere online?'
    )
    earliest_date = models.DateField(
        null=True, blank=True, help_text='Earliest date the media appeared online, if found'
    )
    matched_url = models.URLField(blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        outcome = 'match found' if self.result_found else 'no match'
        return f'{self.get_engine_display()}: {outcome}'