from django.db import transaction
from django.db.models import F
from rest_framework import serializers

from .models import Event, Reservation


class EventSerializer(serializers.ModelSerializer):
    reservations_count = serializers.SerializerMethodField()

    class Meta:
        model = Event
        fields = [
            'id', 'title', 'venue', 'date', 'total_seats', 'available_seats',
            'status', 'created_at', 'reservations_count',
        ]
        read_only_fields = ['created_at', 'reservations_count']

    def get_reservations_count(self, obj):
        return obj.reservations.filter(status='confirmed').count()

    def validate(self, data):
        total_seats = data.get('total_seats', getattr(self.instance, 'total_seats', None))
        available_seats = data.get(
            'available_seats', getattr(self.instance, 'available_seats', None)
        )
        if (
            total_seats is not None
            and available_seats is not None
            and available_seats > total_seats
        ):
            raise serializers.ValidationError(
                'available_seats cannot exceed total_seats.'
            )
        return data


class ReservationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Reservation
        fields = [
            'id', 'event', 'attendee_name', 'attendee_email',
            'seats_reserved', 'status', 'created_at',
        ]
        read_only_fields = ['status', 'created_at']

    def validate_seats_reserved(self, value):
        if value < 1:
            raise serializers.ValidationError('Must reserve at least 1 seat.')
        return value

    def validate(self, data):
        event = data.get('event', self.instance.event if self.instance else None)
        seats_reserved = data.get(
            'seats_reserved', self.instance.seats_reserved if self.instance else 0
        )
        if event is None:
            return data

        if event.status not in ('upcoming', 'ongoing'):
            raise serializers.ValidationError(
                f'Cannot reserve seats for a {event.status} event.'
            )

        available_seats = event.available_seats
        if (
            self.instance
            and self.instance.status == 'confirmed'
            and self.instance.event_id == event.id
        ):
            available_seats += self.instance.seats_reserved
        if seats_reserved > available_seats:
            raise serializers.ValidationError(
                f'Only {available_seats} seat(s) available.'
            )
        return data

    @staticmethod
    def _reserve_from_event(event, seats_reserved):
        updated = Event.objects.filter(
            pk=event.pk,
            status__in=['upcoming', 'ongoing'],
            available_seats__gte=seats_reserved,
        ).update(available_seats=F('available_seats') - seats_reserved)
        if updated:
            return

        current_event = Event.objects.get(pk=event.pk)
        if current_event.status not in ('upcoming', 'ongoing'):
            raise serializers.ValidationError(
                f'Cannot reserve seats for a {current_event.status} event.'
            )
        raise serializers.ValidationError(
            f'Only {current_event.available_seats} seat(s) available.'
        )

    @transaction.atomic
    def create(self, validated_data):
        event = validated_data['event']
        self._reserve_from_event(event, validated_data['seats_reserved'])
        return Reservation.objects.create(**validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        old_event = instance.event
        old_seats = instance.seats_reserved
        new_event = validated_data.get('event', old_event)
        new_seats = validated_data.get('seats_reserved', old_seats)

        if instance.status == 'confirmed':
            if new_event.pk == old_event.pk:
                seat_delta = new_seats - old_seats
                if seat_delta > 0:
                    self._reserve_from_event(new_event, seat_delta)
                elif seat_delta < 0:
                    Event.objects.filter(pk=old_event.pk).update(
                        available_seats=F('available_seats') - seat_delta
                    )
            else:
                self._reserve_from_event(new_event, new_seats)
                Event.objects.filter(pk=old_event.pk).update(
                    available_seats=F('available_seats') + old_seats
                )

        return super().update(instance, validated_data)
