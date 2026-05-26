from rest_framework import serializers
from .models import UserProfile


class UserProfileSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source='user.email', read_only=True)

    class Meta:
        model = UserProfile
        fields = ('email', 'sheet_id', 'telegram_chat_id', 'telegram_enabled', 'selected_tab_names')

    def validate_sheet_id(self, value):
        value = value.strip()
        # Accept either the full URL or just the ID
        if 'spreadsheets/d/' in value:
            try:
                value = value.split('spreadsheets/d/')[1].split('/')[0]
            except IndexError:
                raise serializers.ValidationError("URL da planilha inválida.")
        return value
