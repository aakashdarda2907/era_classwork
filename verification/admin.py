from django.contrib import admin
from django.utils.html import format_html

from .models import Evidence, ReverseSearchLog, VerificationCase


class EvidenceInline(admin.TabularInline):
    model = Evidence
    extra = 1
    fields = ('evidence_type', 'title', 'url', 'stance', 'note')


class ReverseSearchLogInline(admin.TabularInline):
    model = ReverseSearchLog
    extra = 1
    fields = ('engine', 'result_found', 'earliest_date', 'matched_url', 'notes')


@admin.register(VerificationCase)
class VerificationCaseAdmin(admin.ModelAdmin):
    list_display = (
        'tracking_id', 'title', 'media_type', 'status',
        'score_badge', 'evidence_count', 'created_at',
    )
    list_filter = ('status', 'media_type', 'created_at')
    search_fields = ('title', 'claim_description', 'claimed_source', 'source_url')
    date_hierarchy = 'created_at'
    inlines = [ReverseSearchLogInline, EvidenceInline]
    readonly_fields = ('tracking_id', 'score_summary', 'ai_analyzed_at', 'created_at', 'updated_at')
    actions = ['mark_verifying', 'mark_concluded']

    fieldsets = (
        ('Case', {
            'fields': ('tracking_id', 'title', 'claim_description', 'media_file', 'media_type', 'status'),
        }),
        ('Source and provenance', {
            'fields': ('claimed_source', 'source_url', 'publication_date', 'provenance_notes'),
        }),
        ('Assessment', {
            'fields': ('score_summary', 'ai_analysis', 'ai_analyzed_at'),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Score')
    def score_badge(self, obj):
        result = obj.credibility
        colors = {'good': '#1a7f37', 'warn': '#9a6700', 'bad': '#cf222e'}
        return format_html(
            '<b style="color:{}">{}/100</b> {}',
            colors.get(result['css_class'], '#555'),
            result['score'],
            result['verdict'],
        )

    @admin.display(description='Credibility (guidance only)')
    def score_summary(self, obj):
        if not obj.pk:
            return 'Save the case first to see its score.'
        result = obj.credibility
        return f"{result['score']}/100: {result['verdict']}"

    @admin.display(description='Evidence')
    def evidence_count(self, obj):
        return obj.evidence_items.count()

    @admin.action(description='Mark selected cases as Under Verification')
    def mark_verifying(self, request, queryset):
        updated = queryset.update(status=VerificationCase.STATUS_VERIFYING)
        self.message_user(request, f'{updated} case(s) marked as Under Verification.')

    @admin.action(description='Mark selected cases as Concluded')
    def mark_concluded(self, request, queryset):
        updated = queryset.update(status=VerificationCase.STATUS_CONCLUDED)
        self.message_user(request, f'{updated} case(s) marked as Concluded.')


@admin.register(Evidence)
class EvidenceAdmin(admin.ModelAdmin):
    list_display = ('title', 'case', 'evidence_type', 'stance', 'created_at')
    list_filter = ('evidence_type', 'stance')
    search_fields = ('title', 'note', 'url')


@admin.register(ReverseSearchLog)
class ReverseSearchLogAdmin(admin.ModelAdmin):
    list_display = ('case', 'engine', 'result_found', 'earliest_date', 'created_at')
    list_filter = ('engine', 'result_found')
    search_fields = ('notes', 'matched_url')