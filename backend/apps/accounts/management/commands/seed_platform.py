import uuid
from decimal import Decimal
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction

from apps.accounts.models import User, ShelterProfile, DeliveryProfile
from apps.pets.models import Pet, PetImage, PetMedicalRecord
from apps.adoptions.models import AdoptionApplication, AdoptionReview
from apps.logistics.models import PetDelivery, DeliveryLocationPing, Payment


class Command(BaseCommand):
    help = "Seeds database with realistic initial users, shelters, fleet drivers, pets, and live deliveries."

    @transaction.atomic
    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("Seeding KindHeart Pet Adoption & Logistics Platform..."))

        # -------------------------------------------------------------------------
        # 1. Admin Account
        # -------------------------------------------------------------------------
        admin_user, _ = User.objects.get_or_create(
            email='admin@kindheart.local',
            defaults={
                'username': 'admin_kindheart',
                'first_name': 'Platform',
                'last_name': 'Administrator',
                'role': 'admin',
                'is_staff': True,
                'is_superuser': True,
            }
        )
        admin_user.set_password('AdminPass123!')
        admin_user.role = 'admin'
        admin_user.is_staff = True
        admin_user.is_superuser = True
        admin_user.save()

        # -------------------------------------------------------------------------
        # 2. Shelters
        # -------------------------------------------------------------------------
        # Shelter 1: Kochi
        kochi_user, _ = User.objects.get_or_create(
            email='shelter.kochi@kindheart.local',
            defaults={
                'username': 'shelter_kochi',
                'first_name': 'Kochi',
                'last_name': 'Shelter Admin',
                'phone': '+919847012345',
                'role': 'shelter',
            }
        )
        kochi_user.set_password('ShelterPass123!')
        kochi_user.role = 'shelter'
        kochi_user.save()

        shelter_kochi, _ = ShelterProfile.objects.update_or_create(
            user=kochi_user,
            defaults={
                'organization_name': 'KindHeart Kochi Haven',
                'license_no': 'KL-ERM-SH-2026-001',
                'pickup_address': 'Marine Drive Walkway, Ernakulam, Kochi, Kerala 682031',
                'city': 'Kochi',
                'latitude': Decimal('9.981600'),
                'longitude': Decimal('76.271100'),
                'is_active': True,
                'is_verified': True,
            }
        )

        # Shelter 2: Bangalore
        blr_user, _ = User.objects.get_or_create(
            email='shelter.blr@kindheart.local',
            defaults={
                'username': 'shelter_blr',
                'first_name': 'Bangalore',
                'last_name': 'Shelter Admin',
                'phone': '+919880012345',
                'role': 'shelter',
            }
        )
        blr_user.set_password('ShelterPass123!')
        blr_user.role = 'shelter'
        blr_user.save()

        shelter_blr, _ = ShelterProfile.objects.update_or_create(
            user=blr_user,
            defaults={
                'organization_name': 'KindHeart Bangalore Rescue',
                'license_no': 'KA-BLR-SH-2026-002',
                'pickup_address': '100ft Road, Indiranagar, Bangalore, Karnataka 560038',
                'city': 'Bangalore',
                'latitude': Decimal('12.971600'),
                'longitude': Decimal('77.594600'),
                'is_active': True,
                'is_verified': True,
            }
        )

        # -------------------------------------------------------------------------
        # 3. Delivery Partners
        # -------------------------------------------------------------------------
        # Driver 1 - Kochi
        driver1_user, _ = User.objects.get_or_create(
            email='driver.kochi@kindheart.local',
            defaults={
                'username': 'driver_kochi',
                'first_name': 'Vipin',
                'last_name': 'Nair',
                'phone': '+919847111222',
                'role': 'delivery',
            }
        )
        driver1_user.set_password('DriverPass123!')
        driver1_user.role = 'delivery'
        driver1_user.save()

        driver1_profile, _ = DeliveryProfile.objects.update_or_create(
            user=driver1_user,
            defaults={
                'shelter': shelter_kochi,
                'vehicle_type': 'car',
                'vehicle_number': 'KL-07-CD-1234',
                'is_available': True,
                'current_latitude': Decimal('9.982000'),
                'current_longitude': Decimal('76.272000'),
            }
        )

        # Driver 2 - Bangalore (Active delivery driver)
        driver2_user, _ = User.objects.get_or_create(
            email='driver.blr1@kindheart.local',
            defaults={
                'username': 'driver_blr1',
                'first_name': 'Ramesh',
                'last_name': 'Gowda',
                'phone': '+919880111222',
                'role': 'delivery',
            }
        )
        driver2_user.set_password('DriverPass123!')
        driver2_user.role = 'delivery'
        driver2_user.save()

        driver2_profile, _ = DeliveryProfile.objects.update_or_create(
            user=driver2_user,
            defaults={
                'shelter': shelter_blr,
                'vehicle_type': 'two_wheeler',
                'vehicle_number': 'KA-03-AB-5678',
                'is_available': False,
                'current_latitude': Decimal('12.972000'),
                'current_longitude': Decimal('77.600000'),
            }
        )

        # Driver 3 - Bangalore (Available)
        driver3_user, _ = User.objects.get_or_create(
            email='driver.blr2@kindheart.local',
            defaults={
                'username': 'driver_blr2',
                'first_name': 'Sunil',
                'last_name': 'Kumar',
                'phone': '+919880222333',
                'role': 'delivery',
            }
        )
        driver3_user.set_password('DriverPass123!')
        driver3_user.role = 'delivery'
        driver3_user.save()

        DeliveryProfile.objects.update_or_create(
            user=driver3_user,
            defaults={
                'shelter': shelter_blr,
                'vehicle_type': 'van',
                'vehicle_number': 'KA-01-EF-9012',
                'is_available': True,
                'current_latitude': Decimal('12.975000'),
                'current_longitude': Decimal('77.605000'),
            }
        )

        # -------------------------------------------------------------------------
        # 4. Customers
        # -------------------------------------------------------------------------
        customer1, _ = User.objects.get_or_create(
            email='customer.priya@example.com',
            defaults={
                'username': 'priya_sharma',
                'first_name': 'Priya',
                'last_name': 'Sharma',
                'phone': '+919876511111',
                'address': 'Flat 402, Green Glen Layout, Bellandur, Bangalore',
                'role': 'customer',
            }
        )
        customer1.set_password('CustomerPass123!')
        customer1.role = 'customer'
        customer1.save()

        customer2, _ = User.objects.get_or_create(
            email='customer.rahul@example.com',
            defaults={
                'username': 'rahul_nair',
                'first_name': 'Rahul',
                'last_name': 'Nair',
                'phone': '+919876522222',
                'address': '202 Panampilly Nagar, Kochi, Kerala',
                'role': 'customer',
            }
        )
        customer2.set_password('CustomerPass123!')
        customer2.role = 'customer'
        customer2.save()

        # -------------------------------------------------------------------------
        # 5. Populate 10 Pets across Dogs & Cats
        # -------------------------------------------------------------------------
        pets_data = [
            # Bangalore Pets
            {
                'shelter': shelter_blr,
                'name': 'Buddy',
                'species': 'dog',
                'breed': 'Golden Retriever',
                'age_months': 18,
                'gender': 'male',
                'size': 'large',
                'description': 'Energetic, loyal, and friendly golden retriever who loves walks.',
                'fee': Decimal('150.00'),
                'status': 'available',
                'img': 'https://images.unsplash.com/photo-1552053831-71594a27632d',
                'med_title': 'Core Vaccinations',
                'med_desc': 'Annual Rabies & DHPP completed.',
            },
            {
                'shelter': shelter_blr,
                'name': 'Bella',
                'species': 'dog',
                'breed': 'Beagle',
                'age_months': 8,
                'gender': 'female',
                'size': 'small',
                'description': 'Curious and happy beagle puppy with great social skills.',
                'fee': Decimal('120.00'),
                'status': 'available',
                'img': 'https://images.unsplash.com/photo-1537151625747-768eb6cf92b2',
                'med_title': 'Puppy Immunization',
                'med_desc': 'Parvo and deworming schedule up-to-date.',
            },
            {
                'shelter': shelter_blr,
                'name': 'Snowy',
                'species': 'cat',
                'breed': 'Persian',
                'age_months': 14,
                'gender': 'female',
                'size': 'small',
                'description': 'Gentle, calm white persian cat looking for an apartment home.',
                'fee': Decimal('100.00'),
                'status': 'available',
                'img': 'https://images.unsplash.com/photo-1514888286974-6c03e2ca1dba',
                'med_title': 'Feline Health Check',
                'med_desc': 'FVRCP and rabies administered.',
            },
            {
                'shelter': shelter_blr,
                'name': 'Rocky',
                'species': 'dog',
                'breed': 'Labrador',
                'age_months': 24,
                'gender': 'male',
                'size': 'large',
                'description': 'Trained guide labrador, wonderfully calm temperament.',
                'fee': Decimal('160.00'),
                'status': 'available',
                'img': 'https://images.unsplash.com/photo-1587300003388-59208cc962cb',
                'med_title': 'Full Medical Clearance',
                'med_desc': 'Microchipped, neutered, and fully vaccinated.',
            },
            {
                'shelter': shelter_blr,
                'name': 'Leo',
                'species': 'dog',
                'breed': 'Indie',
                'age_months': 6,
                'gender': 'male',
                'size': 'medium',
                'description': 'Rescued indie pup, intelligent, alert, and great with kids.',
                'fee': Decimal('50.00'),
                'status': 'available',
                'img': 'https://images.unsplash.com/photo-1543466835-00a7907e9de1',
                'med_title': 'Intake Deworming',
                'med_desc': 'Primary vaccines and deworming done.',
            },
            # Kochi Pets
            {
                'shelter': shelter_kochi,
                'name': 'Simba',
                'species': 'cat',
                'breed': 'Siamese',
                'age_months': 12,
                'gender': 'male',
                'size': 'small',
                'description': 'Vocal and affectionate siamese cat with striking blue eyes.',
                'fee': Decimal('90.00'),
                'status': 'available',
                'img': 'https://images.unsplash.com/photo-1513360309081-38f076278f1d',
                'med_title': 'Feline Deworming',
                'med_desc': 'Triple dewormer administered.',
            },
            {
                'shelter': shelter_kochi,
                'name': 'Max',
                'species': 'dog',
                'breed': 'German Shepherd',
                'age_months': 30,
                'gender': 'male',
                'size': 'large',
                'description': 'Protective and obedient german shepherd companion.',
                'fee': Decimal('180.00'),
                'status': 'available',
                'img': 'https://images.unsplash.com/photo-1589941013453-ec89f33b5455',
                'med_title': 'Hip Dysplasia & Core Vaccines',
                'med_desc': 'Normal joint screening, core vaccine boosters complete.',
            },
            {
                'shelter': shelter_kochi,
                'name': 'Cleo',
                'species': 'cat',
                'breed': 'Indie',
                'age_months': 9,
                'gender': 'female',
                'size': 'small',
                'description': 'Playful rescued domestic shorthair kitten.',
                'fee': Decimal('60.00'),
                'status': 'available',
                'img': 'https://images.unsplash.com/photo-1573865526739-10659fec78a5',
                'med_title': 'Spay & Vaccination',
                'med_desc': 'Sterilized and vaccinated at Kochi District Vet Hospital.',
            },
            {
                'shelter': shelter_kochi,
                'name': 'Daisy',
                'species': 'dog',
                'breed': 'Cocker Spaniel',
                'age_months': 16,
                'gender': 'female',
                'size': 'medium',
                'description': 'Sweet cocker spaniel who adores outdoor play and swimming.',
                'fee': Decimal('140.00'),
                'status': 'available',
                'img': 'https://images.unsplash.com/photo-1518020382113-a7e8fc38eac9',
                'med_title': 'Ear Care & Immunization',
                'med_desc': 'Preventive ear flush and anti-tick treatment.',
            },
            {
                'shelter': shelter_kochi,
                'name': 'Shadow',
                'species': 'cat',
                'breed': 'Maine Coon',
                'age_months': 22,
                'gender': 'male',
                'size': 'large',
                'description': 'Fluffy and majestic maine coon mix with playful habits.',
                'fee': Decimal('110.00'),
                'status': 'available',
                'img': 'https://images.unsplash.com/photo-1533738363-b7f9aef128ce',
                'med_title': 'Dental & General Health',
                'med_desc': 'Dental scaling completed, heartworm test negative.',
            },
        ]

        seeded_pets = []
        for p in pets_data:
            pet, _ = Pet.objects.update_or_create(
                shelter=p['shelter'],
                name=p['name'],
                defaults={
                    'species': p['species'],
                    'breed': p['breed'],
                    'age_months': p['age_months'],
                    'gender': p['gender'],
                    'size': p['size'],
                    'description': p['description'],
                    'adoption_fee': p['fee'],
                    'is_vaccinated': True,
                    'is_neutered': True,
                    'status': p['status'],
                }
            )
            # Add or update primary image
            PetImage.objects.update_or_create(
                pet=pet,
                is_primary=True,
                defaults={
                    'image_url': p['img'],
                    'caption': f"{pet.name} portrait",
                }
            )
            # Add medical record
            PetMedicalRecord.objects.get_or_create(
                pet=pet,
                title=p['med_title'],
                defaults={
                    'description': p['med_desc'],
                    'vet_name': 'Dr. K. S. Pillai, BVSc',
                    'record_date': timezone.now().date() - timedelta(days=30),
                }
            )
            seeded_pets.append(pet)

        # -------------------------------------------------------------------------
        # 6. Active Delivery Task with OTPs & GPS Pings
        # -------------------------------------------------------------------------
        # Adoption Application for Rocky by Customer 1 (Priya)
        rocky_pet = seeded_pets[3]
        app, _ = AdoptionApplication.objects.update_or_create(
            applicant=customer1,
            pet=rocky_pet,
            defaults={
                'status': 'approved',
                'living_situation': 'Spacious independent house with yard',
                'has_other_pets': False,
                'contact_phone': customer1.phone,
                'application_notes': 'Spacious home with enclosed yard, experienced with labradors.',
                'dropoff_address': customer1.address,
                'delivery_address': customer1.address,
                'delivery_city': 'Bangalore',
                'delivery_pincode': '560103',
                'destination_latitude': Decimal('12.935200'),
                'destination_longitude': Decimal('77.624500'),
                'customer_latitude': Decimal('12.935200'),
                'customer_longitude': Decimal('77.624500'),
                'distance_km': Decimal('6.80'),
                'distance_fee': Decimal('68.00'),
                'pet_care_fee': rocky_pet.adoption_fee,
                'base_delivery_fee': Decimal('50.00'),
                'total_amount': rocky_pet.adoption_fee + Decimal('118.00'),
            }
        )

        # Active Delivery
        active_delivery, _ = PetDelivery.objects.update_or_create(
            application=app,
            defaults={
                'driver': driver2_profile,
                'status': 'out_for_delivery',
                'pickup_otp': '582910',
                'doorstep_otp': '419283',
                'secret_otp': '419283',
                'current_latitude': Decimal('12.960000'),
                'current_longitude': Decimal('77.610000'),
                'dispatched_at': timezone.now() - timedelta(minutes=25),
                'last_location_update': timezone.now() - timedelta(minutes=2),
                'scheduled_date': timezone.now() + timedelta(hours=2),
                'payment_status': 'paid',
            }
        )

        # 3 Location Pings for Real-Time Tracking
        DeliveryLocationPing.objects.filter(delivery=active_delivery).delete()
        DeliveryLocationPing.objects.create(
            delivery=active_delivery,
            latitude=Decimal('12.971600'),
            longitude=Decimal('77.594600'),
        )
        DeliveryLocationPing.objects.create(
            delivery=active_delivery,
            latitude=Decimal('12.960000'),
            longitude=Decimal('77.610000'),
        )
        DeliveryLocationPing.objects.create(
            delivery=active_delivery,
            latitude=Decimal('12.948000'),
            longitude=Decimal('77.618000'),
        )

        # Payment record
        Payment.objects.update_or_create(
            delivery=active_delivery,
            defaults={
                'application': app,
                'amount_in_paise': 27800,  # ₹278.00
                'amount': Decimal('278.00'),
                'payment_method': 'online',
                'status': 'paid',
                'razorpay_order_id': 'order_live_seed_001',
                'razorpay_payment_id': 'pay_live_seed_001',
                'razorpay_signature': 'sig_verified_seed_001',
            }
        )

        # Past completed application & reviews to populate ratings
        past_pet = seeded_pets[5]  # Simba
        past_app, _ = AdoptionApplication.objects.update_or_create(
            applicant=customer2,
            pet=past_pet,
            defaults={
                'status': 'completed',
                'living_situation': 'Apartment with pet-friendly society',
                'has_other_pets': False,
                'contact_phone': customer2.phone,
                'application_notes': 'Loving home with gentle care.',
                'dropoff_address': customer2.address,
                'delivery_address': customer2.address,
                'delivery_city': 'Kochi',
                'delivery_pincode': '682036',
                'destination_latitude': Decimal('9.965000'),
                'destination_longitude': Decimal('76.280000'),
                'customer_latitude': Decimal('9.965000'),
                'customer_longitude': Decimal('76.280000'),
                'distance_km': Decimal('3.50'),
                'distance_fee': Decimal('35.00'),
                'pet_care_fee': past_pet.adoption_fee,
                'base_delivery_fee': Decimal('50.00'),
                'total_amount': past_pet.adoption_fee + Decimal('85.00'),
            }
        )

        past_delivery, _ = PetDelivery.objects.update_or_create(
            application=past_app,
            defaults={
                'driver': driver1_profile,
                'status': 'delivered',
                'pickup_otp': '111222',
                'doorstep_otp': '333444',
                'secret_otp': '333444',
                'current_latitude': Decimal('9.965000'),
                'current_longitude': Decimal('76.280000'),
                'dispatched_at': timezone.now() - timedelta(days=6),
                'delivered_at': timezone.now() - timedelta(days=5),
                'payment_status': 'paid',
            }
        )

        AdoptionReview.objects.update_or_create(
            application=past_app,
            defaults={
                'reviewer': customer2,
                'shelter_rating': 5,
                'driver_rating': 5,
                'shelter_review': 'Exemplary shelter care and health support!',
                'driver_review': 'Fast and courteous doorstep pet transport!',
            }
        )

        # -------------------------------------------------------------------------
        # Clean Terminal Summary (Secrets/Passwords are NOT displayed raw)
        # -------------------------------------------------------------------------
        self.stdout.write(self.style.SUCCESS("\n" + "=" * 70))
        self.stdout.write(self.style.SUCCESS("  KindHeart Pet Adoption Platform - Database Seeding Complete"))
        self.stdout.write(self.style.SUCCESS("=" * 70))
        self.stdout.write(self.style.NOTICE("[1] System Administrator:"))
        self.stdout.write(f"    - Email: {admin_user.email} (Role: {admin_user.role}) [PASSWORD SECURELY CONFIGURED]")
        self.stdout.write(self.style.NOTICE("[2] Shelters (2 profiles):"))
        self.stdout.write(f"    - {shelter_kochi.organization_name} ({shelter_kochi.city}) -> {kochi_user.email} [PASSWORD SECURELY CONFIGURED]")
        self.stdout.write(f"    - {shelter_blr.organization_name} ({shelter_blr.city}) -> {blr_user.email} [PASSWORD SECURELY CONFIGURED]")
        self.stdout.write(self.style.NOTICE("[3] Delivery Partners (3 fleet drivers):"))
        self.stdout.write(f"    - {driver1_user.email} (KL-07-CD-1234, Car) [PASSWORD SECURELY CONFIGURED]")
        self.stdout.write(f"    - {driver2_user.email} (KA-03-AB-5678, Two-Wheeler) [PASSWORD SECURELY CONFIGURED]")
        self.stdout.write(f"    - {driver3_user.email} (KA-01-EF-9012, Van) [PASSWORD SECURELY CONFIGURED]")
        self.stdout.write(self.style.NOTICE("[4] Customers (2 users):"))
        self.stdout.write(f"    - {customer1.email} ({customer1.phone}) [PASSWORD SECURELY CONFIGURED]")
        self.stdout.write(f"    - {customer2.email} ({customer2.phone}) [PASSWORD SECURELY CONFIGURED]")
        self.stdout.write(self.style.NOTICE(f"[5] Pets Catalog: {len(seeded_pets)} pets populated with images and health records."))
        self.stdout.write(self.style.NOTICE(f"[6] Active Delivery Task: 1 delivery 'out_for_delivery' with 3 GPS pings."))
        self.stdout.write(self.style.SUCCESS("=" * 70 + "\n"))
